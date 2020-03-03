from pathlib import Path
from typing import Union

import torch.nn as nn
import torch
import torch.nn.functional as F

from models.tacotron import CBHG


class LengthRegulator(nn.Module):

    def __init__(self):
        super().__init__()

    def forward(self, x, dur):
        output = []
        for x_i, dur_i in zip(x, dur):
            expanded = self.expand(x_i, dur_i)
            output.append(expanded)
        output = self.pad(output)
        return output

    def expand(self, x, dur):
        output = []
        for i, frame in enumerate(x):
            expanded_len = int(dur[i] + 0.5)
            expanded = frame.expand(expanded_len, -1)
            output.append(expanded)
        output = torch.cat(output, 0)
        return output

    def pad(self, x):
        output = []
        max_len = max([x[i].size(0) for i in range(len(x))])
        for i, seq in enumerate(x):
            padded = F.pad(seq, [0, 0, 0, max_len - seq.size(0)], "constant", 0.0)
            output.append(padded)
        output = torch.stack(output)
        return output


class DurationPredictor(nn.Module):

    def __init__(self, in_dim, conv_dim=256):
        super().__init__()
        self.convs = torch.nn.ModuleList([
            BatchNormConv(in_dim, conv_dim, 5, activation=torch.relu),
            BatchNormConv(conv_dim, conv_dim, 5, activation=torch.relu),
            BatchNormConv(conv_dim, conv_dim, 5, activation=torch.relu),
        ])
        self.lin = nn.Linear(conv_dim, 1)

    def forward(self, x, alpha=1.0):
        x = x.transpose(1, 2)
        for conv in self.convs:
            x = conv(x)
        x = x.transpose(1, 2)
        x = self.lin(x)
        return x * alpha


class BatchNormConv(nn.Module):

    def __init__(self, in_channels, out_channels, kernel, activation=None):
        super().__init__()
        self.conv = nn.Conv1d(in_channels, out_channels, kernel, stride=1, padding=kernel // 2, bias=False)
        self.bnorm = nn.BatchNorm1d(out_channels)
        self.activation = activation

    def forward(self, x):
        x = self.conv(x)
        if self.activation:
            x = self.activation(x)
        x = self.bnorm(x)
        return x


class Postnet(nn.Module):

    def __init__(self, in_dim, conv_dim, mels):
        super().__init__()
        self.lin = torch.nn.Linear(in_dim, mels)
        self.convs = torch.nn.ModuleList([
            BatchNormConv(in_dim, conv_dim, 5, activation=torch.tanh),
            BatchNormConv(conv_dim, conv_dim, 5, activation=torch.tanh),
            BatchNormConv(conv_dim, conv_dim, 5, activation=torch.tanh),
            BatchNormConv(conv_dim, conv_dim, 5, activation=torch.tanh),
            BatchNormConv(conv_dim, mels, 5),
        ])

    def forward(self, x):
        for conv in self.convs:
            x = conv(x)
            x = F.dropout(x, training=self.training)
        return x


class LightTTS(nn.Module):

    def __init__(self,
                 embed_dims,
                 num_chars,
                 lstm_dims,
                 prenet_k,
                 prenet_dims,
                 postnet_k,
                 postnet_dims,
                 num_highways,
                 n_mels=80):
        super().__init__()
        self._to_flatten = []
        self.lstm_dims = lstm_dims

        self.embedding = nn.Embedding(num_chars, embed_dims)
        self.prenet = CBHG(K=prenet_k,
                           in_channels=embed_dims,
                           channels=prenet_dims,
                           proj_channels=[prenet_dims, embed_dims],
                           num_highways=num_highways)
        self.lr = LengthRegulator()
        self.dur_pred = DurationPredictor(embed_dims, num_chars)
        self.lstm = nn.LSTM(2 * embed_dims,
                            lstm_dims,
                            batch_first=True,
                            bidirectional=True)
        self._to_flatten.append(self.lstm)
        self.lin = torch.nn.Linear(2 * lstm_dims, n_mels)
        self.register_buffer('step', torch.zeros(1, dtype=torch.long))
        self.postnet = CBHG(K=postnet_k,
                            in_channels=n_mels,
                            channels=postnet_dims,
                            proj_channels=[postnet_dims, n_mels],
                            num_highways=num_highways)
        self.post_proj = nn.Linear(2 * postnet_dims, n_mels, bias=False)

        # Avoid fragmentation of RNN parameters and associated warning
        self._flatten_parameters()

    def forward(self, x, mel, dur):
        self._flatten_parameters()
        self.train()
        self.step += 1

        x = self.embedding(x)
        dur_hat = self.dur_pred(x)
        dur_hat = dur_hat.squeeze()

        x = x.transpose(1, 2)
        x = self.prenet(x)
        x = self.lr(x, dur)
        x, _ = self.lstm(x)
        x = self.lin(x)
        x = x.transpose(1, 2)

        x_post = self.postnet(x)
        x_post = self.post_proj(x_post)
        x_post = x_post.transpose(1, 2)

        x_post = self.pad(x_post, mel.size(2))
        x = self.pad(x, mel.size(2))

        return x, x_post, dur_hat

    def generate(self, x, alpha=1.0):
        self.eval()
        device = next(self.parameters()).device  # use same device as parameters
        x = torch.as_tensor(x, dtype=torch.long, device=device).unsqueeze(0)

        x = self.embedding(x)
        dur = self.dur_pred(x, alpha=alpha)

        x = x.transpose(1, 2)
        x = self.prenet(x)
        x = self.lr(x, dur)
        x, _ = self.lstm(x)
        x = self.lin(x)
        x = x.transpose(1, 2)

        x_post = self.postnet(x)
        x_post = self.post_proj(x_post)
        x_post = x_post.transpose(1, 2)

        x_post = x_post.squeeze()
        x_post = x_post.cpu().data.numpy()
        return x_post

    def pad(self, x, max_len):
        x = x[:, :, :max_len]
        x = F.pad(x, [0, max_len - x.size(2), 0, 0], "constant", 0.0)
        return x

    def get_step(self):
        return self.step.data.item()

    def load(self, path: Union[str, Path]):
        # Use device of model params as location for loaded state
        device = next(self.parameters()).device
        state_dict = torch.load(path, map_location=device)
        self.load_state_dict(state_dict, strict=False)

    def save(self, path: Union[str, Path]):
        # No optimizer argument because saving a model should not include data
        # only relevant in the training process - it should only be properties
        # of the model itself. Let caller take care of saving optimzier state.
        torch.save(self.state_dict(), path)

    def log(self, path, msg):
        with open(path, 'a') as f:
            print(msg, file=f)

    def _flatten_parameters(self):
        """Calls `flatten_parameters` on all the rnns used by the WaveRNN. Used
        to improve efficiency and avoid PyTorch yelling at us."""
        [m.flatten_parameters() for m in self._to_flatten]