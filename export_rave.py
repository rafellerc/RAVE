import logging
import math
import os

logging.basicConfig(level=logging.INFO)
logging.info("library loading")
import torch

torch.set_grad_enabled(False)

import cached_conv as cc
import gin
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
from effortless_config import Config

import rave
import rave.blocks
import rave.core
import rave.scripted_vq


class ScriptedRAVE(torch.nn.Module):
    def __init__(self, pretrained: rave.RAVE, stereo: bool) -> None:
        super().__init__()
        self.stereo = stereo

        self.pqmf = pretrained.pqmf
        self.encoder = pretrained.encoder
        self.decoder = pretrained.decoder

        self.sr = pretrained.sr

        self.full_latent_size = pretrained.latent_size

        self.register_buffer("latent_pca", pretrained.latent_pca)
        self.register_buffer("latent_mean", pretrained.latent_mean)
        self.register_buffer("fidelity", pretrained.fidelity)

        if isinstance(pretrained.encoder, rave.blocks.VariationalEncoder):
            latent_size = max(
                np.argmax(pretrained.fidelity.numpy() > args.FIDELITY), 1)
            latent_size = 2**math.ceil(math.log2(latent_size))
            self.latent_size = latent_size

        elif isinstance(pretrained.encoder, rave.blocks.DiscreteEncoder):
            self.latent_size = pretrained.encoder.num_quantizers
            self.quantizer = rave.scripted_vq.SimpleQuantizer([
                *map(
                    lambda l: l._codebook.embed,
                    pretrained.encoder.rvq.layers,
                )
            ])
            del self.encoder.rvq

        x = torch.zeros(1, 1, 2**14)
        z = self.encoder(self.pqmf(x))
        ratio_encode = x.shape[-1] // z.shape[-1]

        self.register_buffer(
            "encode_params",
            torch.tensor([1, 1, self.latent_size, ratio_encode]))
        self.register_buffer(
            "decode_params",
            torch.tensor(
                [self.latent_size, ratio_encode, 2 if stereo else 1, 1]))
        self.register_buffer("forward_params",
                             torch.tensor([1, 1, 2 if stereo else 1, 1]))

    def post_process_latent(self, z):
        raise NotImplementedError

    def pre_process_latent(self, z):
        raise NotImplementedError

    @torch.jit.export
    def encode(self, x):
        x = self.pqmf(x)
        z = self.encoder(x)
        z = self.post_process_latent(z)
        return z

    @torch.jit.export
    def decode(self, z):
        if self.stereo:
            z = torch.cat([z, z], 0)

        z = self.pre_process_latent(z)
        y = self.decoder(z)
        y = self.pqmf.inverse(y)

        if self.stereo:
            y = torch.cat(y.chunk(2, 0), 1)

        return y

    def forward(self, x):
        return self.decode(self.encode(x))


class VariationalScriptedRAVE(ScriptedRAVE):
    def post_process_latent(self, z):
        z = self.encoder.reparametrize(z)[0]
        z = z - self.latent_mean.unsqueeze(-1)
        z = F.conv1d(z, self.latent_pca.unsqueeze(-1))
        z = z[:, :self.latent_size]
        return z

    def pre_process_latent(self, z):
        noise = torch.randn(
            z.shape[0],
            self.full_latent_size - self.latent_size,
            z.shape[-1],
        )
        z = torch.cat([z, noise], 1)
        z = F.conv1d(z, self.latent_pca.T.unsqueeze(-1))
        z = z + self.latent_mean.unsqueeze(-1)
        return z


class DiscreteScriptedRAVE(ScriptedRAVE):
    def post_process_latent(self, z):
        z = self.quantizer.residual_quantize(z)
        return z

    def pre_process_latent(self, z):
        z = torch.clamp(z, 0, self.quantizer.n_codes - 1).long()
        z = self.quantizer.residual_dequantize(z)
        z = self.encoder.add_noise_to_vector(z)
        return z


class args(Config):
    NAME = None
    STREAMING = False
    FIDELITY = .95
    STEREO = False


args.parse_args()
cc.use_cached_conv(args.STREAMING)

assert args.NAME is not None, "YOU HAD ONE JOB: GIVE A NAME !!"

logging.info("building rave")

root = os.path.join("runs", args.NAME, "rave")
gin.parse_config_file(os.path.join(root, "config.gin"))
checkpoint = rave.core.search_for_run(root)

pretrained = rave.RAVE()
if checkpoint is not None:
    pretrained.load_state_dict(torch.load(checkpoint)["state_dict"])
else:
    print("No checkpoint found, RAVE will remain randomly initialized")
pretrained.eval()

if isinstance(pretrained.encoder, rave.blocks.VariationalEncoder):
    mode = "variational"
    script_class = VariationalScriptedRAVE
elif isinstance(pretrained.encoder, rave.blocks.DiscreteEncoder):
    mode = "discrete"
    script_class = DiscreteScriptedRAVE
else:
    raise ValueError(f"Encoder type {type(pretrained.encoder)} "
                     "not supported for export.")

logging.info("warmup pass")

x = torch.zeros(1, 1, 2**14)
pretrained(x)

logging.info("remove weightnorm")

for m in pretrained.modules():
    if hasattr(m, "weight_g"):
        nn.utils.remove_weight_norm(m)

logging.info("script model")

scripted_rave = script_class(pretrained=pretrained, stereo=args.STEREO)
scripted_rave = torch.jit.script(scripted_rave)

logging.info("save model")

torch.jit.save(scripted_rave, f"{args.NAME}.ts")

logging.info("check model")

rave.core.check_scripted_model(scripted_rave)
