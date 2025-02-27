import torch
import torch.nn.functional as F
from kantts.utils.audio_torch import stft, MelSpectrogram
from kantts.models.utils import get_mask_from_lengths


class MelReconLoss(torch.nn.Module):
    def __init__(self, loss_type="mae"):
        super(MelReconLoss, self).__init__()
        self.loss_type = loss_type
        if loss_type == "mae":
            self.criterion = torch.nn.L1Loss(reduction="none")
        elif loss_type == "mse":
            self.criterion = torch.nn.MSELoss(reduction="none")
        else:
            raise ValueError("Unknown loss type: {}".format(loss_type))

    def forward(self, output_lengths, mel_targets, dec_outputs, postnet_outputs=None):
        output_masks = get_mask_from_lengths(
            output_lengths, max_len=mel_targets.size(1)
        )
        output_masks = ~output_masks
        valid_outputs = output_masks.sum()

        mel_loss_ = torch.sum(
            self.criterion(mel_targets, dec_outputs) * output_masks.unsqueeze(-1)
        ) / (valid_outputs * mel_targets.size(-1))

        if postnet_outputs is not None:
            mel_loss = torch.sum(
                self.criterion(mel_targets, postnet_outputs)
                * output_masks.unsqueeze(-1)
            ) / (valid_outputs * mel_targets.size(-1))
        else:
            mel_loss = 0.0

        return mel_loss_, mel_loss


class ProsodyReconLoss(torch.nn.Module):
    def __init__(self, loss_type="mae"):
        super(ProsodyReconLoss, self).__init__()
        self.loss_type = loss_type
        if loss_type == "mae":
            self.criterion = torch.nn.L1Loss(reduction="none")
        elif loss_type == "mse":
            self.criterion = torch.nn.MSELoss(reduction="none")
        else:
            raise ValueError("Unknown loss type: {}".format(loss_type))

    def forward(
        self,
        input_lengths,
        duration_targets,
        pitch_targets,
        energy_targets,
        log_duration_predictions,
        pitch_predictions,
        energy_predictions,
    ):
        input_masks = get_mask_from_lengths(
            input_lengths, max_len=duration_targets.size(1)
        )
        input_masks = ~input_masks
        valid_inputs = input_masks.sum()

        dur_loss = (
            torch.sum(
                self.criterion(
                    torch.log(duration_targets.float() + 1), log_duration_predictions
                )
                * input_masks
            )
            / valid_inputs
        )
        pitch_loss = (
            torch.sum(self.criterion(pitch_targets, pitch_predictions) * input_masks)
            / valid_inputs
        )
        energy_loss = (
            torch.sum(self.criterion(energy_targets, energy_predictions) * input_masks)
            / valid_inputs
        )

        return dur_loss, pitch_loss, energy_loss


class GeneratorAdversarialLoss(torch.nn.Module):
    """Generator adversarial loss module."""

    def __init__(
        self,
        average_by_discriminators=True,
        loss_type="mse",
    ):
        """Initialize GeneratorAversarialLoss module."""
        super().__init__()
        self.average_by_discriminators = average_by_discriminators
        assert loss_type in ["mse", "hinge"], f"{loss_type} is not supported."
        if loss_type == "mse":
            self.criterion = self._mse_loss
        else:
            self.criterion = self._hinge_loss

    def forward(self, outputs):
        """Calcualate generator adversarial loss.

        Args:
            outputs (Tensor or list): Discriminator outputs or list of
                discriminator outputs.

        Returns:
            Tensor: Generator adversarial loss value.

        """
        if isinstance(outputs, (tuple, list)):
            adv_loss = 0.0
            for i, outputs_ in enumerate(outputs):
                adv_loss += self.criterion(outputs_)
            if self.average_by_discriminators:
                adv_loss /= i + 1
        else:
            adv_loss = self.criterion(outputs)

        return adv_loss

    def _mse_loss(self, x):
        return F.mse_loss(x, x.new_ones(x.size()))

    def _hinge_loss(self, x):
        return -x.mean()


class DiscriminatorAdversarialLoss(torch.nn.Module):
    """Discriminator adversarial loss module."""

    def __init__(
        self,
        average_by_discriminators=True,
        loss_type="mse",
    ):
        """Initialize DiscriminatorAversarialLoss module."""
        super().__init__()
        self.average_by_discriminators = average_by_discriminators
        assert loss_type in ["mse", "hinge"], f"{loss_type} is not supported."
        if loss_type == "mse":
            self.fake_criterion = self._mse_fake_loss
            self.real_criterion = self._mse_real_loss
        else:
            self.fake_criterion = self._hinge_fake_loss
            self.real_criterion = self._hinge_real_loss

    def forward(self, outputs_hat, outputs):
        """Calcualate discriminator adversarial loss.

        Args:
            outputs_hat (Tensor or list): Discriminator outputs or list of
                discriminator outputs calculated from generator outputs.
            outputs (Tensor or list): Discriminator outputs or list of
                discriminator outputs calculated from groundtruth.

        Returns:
            Tensor: Discriminator real loss value.
            Tensor: Discriminator fake loss value.

        """
        if isinstance(outputs, (tuple, list)):
            real_loss = 0.0
            fake_loss = 0.0
            for i, (outputs_hat_, outputs_) in enumerate(zip(outputs_hat, outputs)):
                if isinstance(outputs_hat_, (tuple, list)):
                    # NOTE(kan-bayashi): case including feature maps
                    outputs_hat_ = outputs_hat_[-1]
                    outputs_ = outputs_[-1]
                real_loss += self.real_criterion(outputs_)
                fake_loss += self.fake_criterion(outputs_hat_)
            if self.average_by_discriminators:
                fake_loss /= i + 1
                real_loss /= i + 1
        else:
            real_loss = self.real_criterion(outputs)
            fake_loss = self.fake_criterion(outputs_hat)

        return real_loss, fake_loss

    def _mse_real_loss(self, x):
        return F.mse_loss(x, x.new_ones(x.size()))

    def _mse_fake_loss(self, x):
        return F.mse_loss(x, x.new_zeros(x.size()))

    def _hinge_real_loss(self, x):
        return -torch.mean(torch.min(x - 1, x.new_zeros(x.size())))

    def _hinge_fake_loss(self, x):
        return -torch.mean(torch.min(-x - 1, x.new_zeros(x.size())))


class FeatureMatchLoss(torch.nn.Module):
    """Feature matching loss module."""

    def __init__(
        self,
        average_by_layers=True,
        average_by_discriminators=True,
    ):
        """Initialize FeatureMatchLoss module."""
        super().__init__()
        self.average_by_layers = average_by_layers
        self.average_by_discriminators = average_by_discriminators

    def forward(self, feats_hat, feats):
        """Calcualate feature matching loss.

        Args:
            feats_hat (list): List of list of discriminator outputs
                calcuated from generater outputs.
            feats (list): List of list of discriminator outputs
                calcuated from groundtruth.

        Returns:
            Tensor: Feature matching loss value.

        """
        feat_match_loss = 0.0
        for i, (feats_hat_, feats_) in enumerate(zip(feats_hat, feats)):
            feat_match_loss_ = 0.0
            for j, (feat_hat_, feat_) in enumerate(zip(feats_hat_, feats_)):
                feat_match_loss_ += F.l1_loss(feat_hat_, feat_.detach())
            if self.average_by_layers:
                feat_match_loss_ /= j + 1
            feat_match_loss += feat_match_loss_
        if self.average_by_discriminators:
            feat_match_loss /= i + 1

        return feat_match_loss


class MelSpectrogramLoss(torch.nn.Module):
    """Mel-spectrogram loss."""

    def __init__(
        self,
        fs=22050,
        fft_size=1024,
        hop_size=256,
        win_length=None,
        window="hann",
        num_mels=80,
        fmin=80,
        fmax=7600,
        center=True,
        normalized=False,
        onesided=True,
        eps=1e-10,
        log_base=10.0,
    ):
        """Initialize Mel-spectrogram loss."""
        super().__init__()
        self.mel_spectrogram = MelSpectrogram(
            fs=fs,
            fft_size=fft_size,
            hop_size=hop_size,
            win_length=win_length,
            window=window,
            num_mels=num_mels,
            fmin=fmin,
            fmax=fmax,
            center=center,
            normalized=normalized,
            onesided=onesided,
            eps=eps,
            log_base=log_base,
        )

    def forward(self, y_hat, y):
        """Calculate Mel-spectrogram loss.

        Args:
            y_hat (Tensor): Generated single tensor (B, 1, T).
            y (Tensor): Groundtruth single tensor (B, 1, T).

        Returns:
            Tensor: Mel-spectrogram loss value.

        """
        mel_hat = self.mel_spectrogram(y_hat)
        mel = self.mel_spectrogram(y)
        mel_loss = F.l1_loss(mel_hat, mel)

        return mel_loss


class SpectralConvergenceLoss(torch.nn.Module):
    """Spectral convergence loss module."""

    def __init__(self):
        """Initilize spectral convergence loss module."""
        super(SpectralConvergenceLoss, self).__init__()

    def forward(self, x_mag, y_mag):
        """Calculate forward propagation.

        Args:
            x_mag (Tensor): Magnitude spectrogram of predicted signal (B, #frames, #freq_bins).
            y_mag (Tensor): Magnitude spectrogram of groundtruth signal (B, #frames, #freq_bins).

        Returns:
            Tensor: Spectral convergence loss value.

        """
        return torch.norm(y_mag - x_mag, p="fro") / torch.norm(y_mag, p="fro")


class LogSTFTMagnitudeLoss(torch.nn.Module):
    """Log STFT magnitude loss module."""

    def __init__(self):
        """Initilize los STFT magnitude loss module."""
        super(LogSTFTMagnitudeLoss, self).__init__()

    def forward(self, x_mag, y_mag):
        """Calculate forward propagation.

        Args:
            x_mag (Tensor): Magnitude spectrogram of predicted signal (B, #frames, #freq_bins).
            y_mag (Tensor): Magnitude spectrogram of groundtruth signal (B, #frames, #freq_bins).

        Returns:
            Tensor: Log STFT magnitude loss value.

        """
        return F.l1_loss(torch.log(y_mag), torch.log(x_mag))


class STFTLoss(torch.nn.Module):
    """STFT loss module."""

    def __init__(
        self, fft_size=1024, shift_size=120, win_length=600, window="hann_window"
    ):
        """Initialize STFT loss module."""
        super(STFTLoss, self).__init__()
        self.fft_size = fft_size
        self.shift_size = shift_size
        self.win_length = win_length
        self.spectral_convergence_loss = SpectralConvergenceLoss()
        self.log_stft_magnitude_loss = LogSTFTMagnitudeLoss()
        # NOTE(kan-bayashi): Use register_buffer to fix #223
        self.register_buffer("window", getattr(torch, window)(win_length))

    def forward(self, x, y):
        """Calculate forward propagation.

        Args:
            x (Tensor): Predicted signal (B, T).
            y (Tensor): Groundtruth signal (B, T).

        Returns:
            Tensor: Spectral convergence loss value.
            Tensor: Log STFT magnitude loss value.

        """
        x_mag = stft(x, self.fft_size, self.shift_size, self.win_length, self.window)
        y_mag = stft(y, self.fft_size, self.shift_size, self.win_length, self.window)
        sc_loss = self.spectral_convergence_loss(x_mag, y_mag)
        mag_loss = self.log_stft_magnitude_loss(x_mag, y_mag)

        return sc_loss, mag_loss


class MultiResolutionSTFTLoss(torch.nn.Module):
    """Multi resolution STFT loss module."""

    def __init__(
        self,
        fft_sizes=[1024, 2048, 512],
        hop_sizes=[120, 240, 50],
        win_lengths=[600, 1200, 240],
        window="hann_window",
    ):
        """Initialize Multi resolution STFT loss module.

        Args:
            fft_sizes (list): List of FFT sizes.
            hop_sizes (list): List of hop sizes.
            win_lengths (list): List of window lengths.
            window (str): Window function type.

        """
        super(MultiResolutionSTFTLoss, self).__init__()
        assert len(fft_sizes) == len(hop_sizes) == len(win_lengths)
        self.stft_losses = torch.nn.ModuleList()
        for fs, ss, wl in zip(fft_sizes, hop_sizes, win_lengths):
            self.stft_losses += [STFTLoss(fs, ss, wl, window)]

    def forward(self, x, y):
        """Calculate forward propagation.

        Args:
            x (Tensor): Predicted signal (B, T) or (B, #subband, T).
            y (Tensor): Groundtruth signal (B, T) or (B, #subband, T).

        Returns:
            Tensor: Multi resolution spectral convergence loss value.
            Tensor: Multi resolution log STFT magnitude loss value.

        """
        if len(x.shape) == 3:
            x = x.view(-1, x.size(2))  # (B, C, T) -> (B x C, T)
            y = y.view(-1, y.size(2))  # (B, C, T) -> (B x C, T)
        sc_loss = 0.0
        mag_loss = 0.0
        for f in self.stft_losses:
            sc_l, mag_l = f(x, y)
            sc_loss += sc_l
            mag_loss += mag_l
        sc_loss /= len(self.stft_losses)
        mag_loss /= len(self.stft_losses)

        return sc_loss, mag_loss


#  TODO: create a mapping for new loss functions
loss_dict = {
    "generator_adv_loss": GeneratorAdversarialLoss,
    "discriminator_adv_loss": DiscriminatorAdversarialLoss,
    "stft_loss": MultiResolutionSTFTLoss,
    "mel_loss": MelSpectrogramLoss,
    "subband_stft_loss": MultiResolutionSTFTLoss,
    "feat_match_loss": FeatureMatchLoss,
    "MelReconLoss": MelReconLoss,
    "ProsodyReconLoss": ProsodyReconLoss,
}


def criterion_builder(config, device="cpu"):
    """Criterion builder.
    Args:
        config (dict): Config dictionary.
    Returns:
        criterion (dict): Loss dictionary
    """
    criterion = {}
    for key, value in config["Loss"].items():
        if key in loss_dict:
            if value["enable"]:
                criterion[key] = loss_dict[key](**value.get("params", {})).to(device)
                setattr(criterion[key], "weights", value.get("weights", 1.0))
        else:
            raise NotImplementedError("{} is not implemented".format(key))

    return criterion
