import torch
from torch.nn.parallel import DistributedDataParallel
from kantts.models.hifigan.hifigan import (  # NOQA
    Generator,  # NOQA
    MultiScaleDiscriminator,  # NOQA
    MultiPeriodDiscriminator,  # NOQA
    MultiSpecDiscriminator,  # NOQA
)
import kantts
import kantts.train.scheduler
from kantts.models.sambert.kantts_sambert import KanTtsSAMBERT  # NOQA
from .pqmf import PQMF


def optimizer_builder(model_params, opt_name, opt_params):
    opt_cls = getattr(torch.optim, opt_name)
    optimizer = opt_cls(model_params, **opt_params)
    return optimizer


def scheduler_builder(optimizer, sche_name, sche_params):
    scheduler_cls = getattr(kantts.train.scheduler, sche_name)
    scheduler = scheduler_cls(optimizer, **sche_params)
    return scheduler


def hifigan_model_builder(config, device, rank, distributed):
    model = {}
    optimizer = {}
    scheduler = {}
    model["discriminator"] = {}
    optimizer["discriminator"] = {}
    scheduler["discriminator"] = {}
    for model_name in config["Model"].keys():
        if model_name == "Generator":
            params = config["Model"][model_name]["params"]
            model["generator"] = Generator(**params).to(device)
            optimizer["generator"] = optimizer_builder(
                model["generator"].parameters(),
                config["Model"][model_name]["optimizer"].get("type", "Adam"),
                config["Model"][model_name]["optimizer"].get("params", {}),
            )
            scheduler["generator"] = scheduler_builder(
                optimizer["generator"],
                config["Model"][model_name]["scheduler"].get("type", "StepLR"),
                config["Model"][model_name]["scheduler"].get("params", {}),
            )
        else:
            params = config["Model"][model_name]["params"]
            model["discriminator"][model_name] = globals()[model_name](**params).to(
                device
            )
            optimizer["discriminator"][model_name] = optimizer_builder(
                model["discriminator"][model_name].parameters(),
                config["Model"][model_name]["optimizer"].get("type", "Adam"),
                config["Model"][model_name]["optimizer"].get("params", {}),
            )
            scheduler["discriminator"][model_name] = scheduler_builder(
                optimizer["discriminator"][model_name],
                config["Model"][model_name]["scheduler"].get("type", "StepLR"),
                config["Model"][model_name]["scheduler"].get("params", {}),
            )

    out_channels = config["Model"]["Generator"]["params"]["out_channels"]
    if out_channels > 1:
        model["pqmf"] = PQMF(subbands=out_channels, **config.get("pqmf", {})).to(device)

    # FIXME: pywavelets buffer leads to gradient error in DDP training
    # Solution: https://github.com/pytorch/pytorch/issues/22095
    if distributed:
        model["generator"] = DistributedDataParallel(
            model["generator"],
            device_ids=[rank],
            output_device=rank,
            broadcast_buffers=False,
        )
        for model_name in model["discriminator"].keys():
            model["discriminator"][model_name] = DistributedDataParallel(
                model["discriminator"][model_name],
                device_ids=[rank],
                output_device=rank,
                broadcast_buffers=False,
            )

    return model, optimizer, scheduler


#  TODO: some parsing
def sambert_model_builder(config, device, rank, distributed):
    model = {}
    optimizer = {}
    scheduler = {}

    model["KanTtsSAMBERT"] = KanTtsSAMBERT(
        config["Model"]["KanTtsSAMBERT"]["params"]
    ).to(device)
    optimizer["KanTtsSAMBERT"] = optimizer_builder(
        model["KanTtsSAMBERT"].parameters(),
        config["Model"]["KanTtsSAMBERT"]["optimizer"].get("type", "Adam"),
        config["Model"]["KanTtsSAMBERT"]["optimizer"].get("params", {}),
    )
    scheduler["KanTtsSAMBERT"] = scheduler_builder(
        optimizer["KanTtsSAMBERT"],
        config["Model"]["KanTtsSAMBERT"]["scheduler"].get("type", "StepLR"),
        config["Model"]["KanTtsSAMBERT"]["scheduler"].get("params", {}),
    )

    if distributed:
        model["KanTtsSAMBERT"] = DistributedDataParallel(
            model["KanTtsSAMBERT"], device_ids=[rank], output_device=rank
        )

    return model, optimizer, scheduler


#  TODO: implement a builder for specific model
model_dict = {
    "hifigan": hifigan_model_builder,
    "sambert": sambert_model_builder,
}


def model_builder(config, device="cpu", rank=0, distributed=False):
    builder_func = model_dict[config["model_type"]]
    model, optimizer, scheduler = builder_func(config, device, rank, distributed)
    return model, optimizer, scheduler
