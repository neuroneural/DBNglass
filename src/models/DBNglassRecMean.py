# pylint: disable=invalid-name, no-member, missing-function-docstring, too-many-branches, too-few-public-methods, unused-argument
""" DICE model from https://github.com/UsmanMahmood27/DICE """
from random import uniform, randint

import torch
from torch import nn
import torch.nn.functional as F
from torch.nn.utils.parametrizations import spectral_norm

from omegaconf import OmegaConf, DictConfig

import ipdb
import time


def get_model(cfg: DictConfig, model_cfg: DictConfig):
    model = MultivariateTSModel(model_cfg)
    if "pretrained" in model_cfg and model_cfg.pretrained:
        pass
        # path = "/data/users2/ppopov1/glass_proj/assets/model_weights/dbn2.pt"
        # checkpoint = torch.load(
        #     path, map_location=lambda storage, loc: storage
        # )
        # model.load_state_dict(checkpoint)
    return model


def get_criterion(cfg: DictConfig, model_cfg: DictConfig):
    return RegCEloss(model_cfg)

class InvertedHoyerMeasure(nn.Module):
    """Sparsity loss function based on Hoyer measure: https://jmlr.csail.mit.edu/papers/volume5/hoyer04a/hoyer04a.pdf"""
    def __init__(self, threshold):
        super(InvertedHoyerMeasure, self).__init__()
        self.threshold = threshold
        self.a = nn.LeakyReLU()

    def forward(self, x):
        # Assuming x has shape (batch_size, input_dim, input_dim)

        n = x[0].numel() # input_dim*input_dim
        sqrt_n = torch.sqrt(torch.tensor(float(n), device=x.device))

        sum_abs_x = torch.sum(torch.abs(x), dim=(1, 2))
        sqrt_sum_squares = torch.sqrt(torch.sum(torch.square(x), dim=(1, 2)))
        numerator = sqrt_n - sum_abs_x / sqrt_sum_squares
        denominator = sqrt_n - 1
        mod_hoyer = 1 - (numerator / denominator) # = 0 if perfectly sparse, 1 if all are equal
        loss = self.a(mod_hoyer - self.threshold)
        # Calculate the mean loss over the batch
        mean_loss = torch.mean(loss)

        return mean_loss


class RegCEloss:
    """Cross-entropy loss with model regularization"""

    def __init__(self, model_cfg):
        self.ce_loss = nn.CrossEntropyLoss()
        self.sparsity_loss = InvertedHoyerMeasure(threshold=model_cfg.loss.threshold)
        self.rec_loss = nn.MSELoss()

        self.labdda = model_cfg.loss.labmda

        self.minimize_global = model_cfg.loss.minimize_global

    def __call__(self, logits, target, model, device, DNC, DNCs):
        ce_loss = self.ce_loss(logits, target)

        # Sparsity loss on DNC
        if self.minimize_global:
            sparse_loss =  self.sparsity_loss(DNC)
        else:
            B, T, C, _ = DNCs.shape
            DNCs = DNCs.reshape(B*T, C, C)
            sparse_loss = self.sparsity_loss(DNCs)

        return ce_loss, self.labdda * sparse_loss
    


def default_HPs(cfg: DictConfig):
    model_cfg = {
        "rnn": {
            "single_embed": True,
            "num_layers": 1,
            "input_embedding_size": 16,
            "hidden_embedding_size": 16,
        },
        "attention": {
            "hidden_dim": 16,
            "track_grads": True,
            "use_tan": "none",
            "use_gate": True,
        },
        "loss": {
            "minimize_global": False,
            "threshold": 0.01,
            "labmda": 1.0,
        },
        "lr": 1e-4,
        "input_size": cfg.dataset.data_info.main.data_shape[2],
        "output_size": cfg.dataset.data_info.main.n_classes,
        "pretrained": True
    }
    return OmegaConf.create(model_cfg)


def random_HPs(cfg: DictConfig, optuna_trial=None):
    model_cfg = {
        "rnn": {
            "single_embed": True,
            "num_layers": 1,
            "input_embedding_size": optuna_trial.suggest_int("rnn.input_embedding_size", 4, 64),
            "hidden_embedding_size": optuna_trial.suggest_int("rnn.hidden_embedding_size", 4, 128),
        },
        "attention": {
            "hidden_dim": optuna_trial.suggest_int("attention.hidden_dim", 4, 64),
            "track_grads": True,
            "use_tan": "none",
            "use_gate": True,
        },
        "loss": {
            "minimize_global": False,
            "threshold": 10 ** optuna_trial.suggest_float("loss.threshold", -2, -0.2),
            "labmda": 10 ** optuna_trial.suggest_float("loss.threshold", -1, 1),
        },
        "lr": 10 ** optuna_trial.suggest_float("lr", -5, -3),
        "input_size": cfg.dataset.data_info.main.data_shape[2],
        "output_size": cfg.dataset.data_info.main.n_classes,
        "pretrained": False
    }
    return OmegaConf.create(model_cfg)

class MultivariateTSModel(nn.Module):
    def __init__(self, model_cfg: DictConfig):
        super(MultivariateTSModel, self).__init__()

        self.num_components = num_components = model_cfg.input_size
        self.num_layers = num_layers = model_cfg.rnn.num_layers
        self.embedding_dim = embedding_dim = model_cfg.rnn.input_embedding_size
        self.hidden_dim = hidden_dim = model_cfg.rnn.hidden_embedding_size
        output_size = model_cfg.output_size

        self.single_embed = model_cfg.rnn.single_embed

        self.prediction_error = nn.MSELoss(reduction='none') # used to evaluate the error of the next predicted input, not the final class prediction
        self.predictor = nn.Linear(hidden_dim, 1)

        # Component-specific embeddings
        if model_cfg.rnn.single_embed:
            self.embeddings = nn.Linear(1, embedding_dim)
        else:
            self.embeddings = nn.ModuleList([
                nn.Linear(1, embedding_dim) for _ in range(num_components)
            ])

        # Recurrent block
        ## GRU layer
        self.gru = nn.GRU(embedding_dim, hidden_dim, num_layers, batch_first=True)

        ## Self-attention layer
        self.attention = SelfAttention(
            input_dim=hidden_dim, 
            hidden_dim=model_cfg.attention.hidden_dim, 
            track_grads=model_cfg.attention.track_grads,
            use_tan=model_cfg.attention.use_tan,
            use_gate=model_cfg.attention.use_gate,
            n_components=self.num_components
        )

        # Global Temporal Attention 
        self.upscale = 0.05
        self.upscale2 = 0.5

        self.gta_embed = nn.Sequential(
            nn.Linear(
                num_components**2,
                round(self.upscale * num_components**2),
            ),
        )
        self.gta_norm = nn.Sequential(
            nn.BatchNorm1d(round(self.upscale * num_components**2)),
            nn.ReLU(),
        )
        self.gta_attend = nn.Sequential(
            nn.Linear(
                round(self.upscale * num_components**2),
                round(self.upscale2 * num_components**2),
            ),
            nn.ReLU(),
            nn.Linear(round(self.upscale2 * num_components**2), 1),
        )

        # Classifier
        self.clf = nn.Sequential(
            nn.Linear(num_components**2, num_components**2 // 2),
            nn.ReLU(),
            nn.Dropout1d(p=0.3),
            nn.Linear(num_components**2 // 2, num_components**2 // 4),
            nn.ReLU(),
            nn.Linear(num_components**2 // 4, output_size),
        )

    def gta_attention(self, x, node_axis=1):
        # x.shape: [batch_size; time_length; input_feature_size * input_feature_size]
        x_readout = x.mean(node_axis, keepdim=True)
        x_readout = x * x_readout

        a = x_readout.shape[0]
        b = x_readout.shape[1]
        x_readout = x_readout.reshape(-1, x_readout.shape[2])
        x_embed = self.gta_norm(self.gta_embed(x_readout))
        x_graphattention = (self.gta_attend(x_embed).squeeze()).reshape(a, b)
        x_graphattention = F.softmax(x_graphattention, dim=1)
        return (x * (x_graphattention.unsqueeze(-1))).sum(node_axis)
    
    def dump_data(self, data, path, basename):
        for i, dat in enumerate(data):
            torch.save(dat, f"{path}/{basename}_{i}.pt")

    def calc_embeddings(self, x):
        # Apply component-specific embeddings
        B, T, C = x.shape
        if self.single_embed:
            x = x.permute(0, 2, 1)
            x = x.reshape(B * self.num_components, T, 1)
            embedded = self.embeddings(x).reshape(B, self.num_components, T, self.embedding_dim)
        else:
            embedded = torch.stack([self.embeddings[i](x[:, :, i].unsqueeze(-1)) for i in range(self.num_components)], dim=1)
        
        return embedded

    def process_step(self, emb, h, B):
        # Recurrent step
        h = h.unsqueeze(1) # prepare h for gru input
        gru_input = emb.permute(0, 2, 1)  # (batch_size, num_components, embedding_dim)
        gru_input = gru_input.reshape(B*self.num_components, 1, self.embedding_dim) # (batch_size * num_components, 1, embedding_dim)
        h = h.permute(1, 0, 2, 3).reshape(1, B*self.num_components, self.hidden_dim) # (1, batch_size * num_components, hidden_dim)
        _, h = self.gru(gru_input, h)
        h = h.reshape(1, B, self.num_components, self.hidden_dim).permute(1, 0, 2, 3) # (batch_size, 1, num_components, hidden_dim)

        # Apply self-attention
        # Reshape h for self-attention
        h = h.squeeze(1)  # (batch_size, num_components, hidden_dim)
        h, mixing_matrix = self.attention(h)
        return h, mixing_matrix

    def forward(self, x, pretraining=False):
        B, T, C = x.shape  # [batch_size, time_length, num_components], C == self.num_components

        # Apply component-specific embeddings
        embedded = self.calc_embeddings(x)
        
        # Initialize hidden state
        h = torch.zeros(B, C, self.hidden_dim, device=x.device)

        mixing_matrices = []
        hidden_states = []
        
        # rec_loss = []
        for t in range(T):
            # Process one time step
            h, mixing_matrix = self.process_step(embedded[:, :, t, :], h, B) # update h with the new input; find new mixing matrix; mix(h)
            hidden_states.append(h)
            mixing_matrices.append(mixing_matrix)
                
            if torch.any(torch.isnan(h)):
                raise Exception(f"h has nans at time point {t}")
            

        mixing_matrices = torch.stack(mixing_matrices, dim=1)  # (batch_size, seq_len, num_components, num_components)
        hidden_states = torch.stack(hidden_states, dim=1)[:, 1:, :, :] #[batch_size; time_length-1; num_components, hidden_dim]
        origs = x[:, 1:, :]
        predicted = self.predictor(hidden_states).squeeze() #[batch_size; time_length-1; num_components]
        rec_loss = self.prediction_error(predicted, origs).mean()
        
        if pretraining:
            return mixing_matrices, rec_loss
        
        clf_input = mixing_matrices.reshape(B, T, -1) # [batch_size; time_length; num_components * num_components]
        logits = self.clf(clf_input)
        logits = torch.mean(logits, dim=1) # mean over time

        DNC = torch.mean(mixing_matrices, dim=1) # mean over time
        
        # Reconstruct the next time points
        return logits, DNC, mixing_matrices, rec_loss


class SelfAttention(nn.Module):
    def __init__(self, input_dim, hidden_dim, track_grads, use_tan, use_gate, n_components):
        super(SelfAttention, self).__init__()
        self.input_dim = input_dim
        self.track_grads = track_grads
        self.use_tan = use_tan
        self.use_gate = use_gate

        if use_gate:
            self.gate = Gate(n_components)

        self.query = nn.Linear(input_dim, hidden_dim)
        self.key = nn.Linear(input_dim, hidden_dim)


    def forward(self, x): # x.shape (batch_size, seq_length, input_dim)
        queries = self.query(x)
        keys = self.key(x)

        transfer = torch.bmm(queries, keys.transpose(1, 2))

        if self.use_tan == "before":
            transfer = F.tanh(transfer)
        
        if self.track_grads:
            norms = torch.linalg.matrix_norm(transfer, keepdim=True)
        else:
            with torch.no_grad():
                norms = torch.linalg.matrix_norm(transfer, keepdim=True).detach()
        transfer = transfer / norms

        if self.use_tan == "after":
            transfer = F.tanh(transfer)

        if self.use_gate:
            gate = self.gate(transfer)
            transfer = transfer * gate

        next_states = torch.bmm(transfer, x)

        return next_states, transfer

class Gate(nn.Module):
    def __init__(self, input_dim):
        super(Gate, self).__init__()
        self.bias = nn.Parameter(torch.randn(input_dim, input_dim))
    
    def forward(self, x):
        # Compute h_ij = abs(x_ij) + b_ij
        h = torch.abs(x) + self.bias
        
        # Compute a_ij = sigmoid(h_ij)
        a = torch.sigmoid(h)
        
        return a