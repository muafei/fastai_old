
        #################################################
        ### THIS FILE WAS AUTOGENERATED! DO NOT EDIT! ###
        #################################################
        # file to edit: dev_nb/007_wikitext_2.ipynb

from nb_005 import *
from collections import Counter

class LanguageModelLoader():
    "Creates a dataloader with bptt slightly changing."
    def __init__(self, nums, bs, bptt, backwards=False):
        self.bs,self.bptt,self.backwards = bs,bptt,backwards
        self.data = self.batchify(nums)
        self.first,self.i,self.iter = True,0,0
        self.n = len(self.data)

    def __iter__(self):
        self.i,self.iter = 0,0
        while self.i < self.n-1 and self.iter<len(self):
            if self.first and self.i == 0: self.first,seq_len = False,self.bptt + 25
            else:
                bptt = self.bptt if np.random.random() < 0.95 else self.bptt / 2.
                seq_len = max(5, int(np.random.normal(bptt, 5)))
            res = self.get_batch(self.i, seq_len)
            self.i += seq_len
            self.iter += 1
            yield res

    def __len__(self): return (self.n-1) // self.bptt

    def batchify(self, data):
        nb = data.shape[0] // self.bs
        data = np.array(data[:nb*self.bs]).reshape(self.bs, -1).T
        if self.backwards: data=data[::-1]
        return LongTensor(data)

    def get_batch(self, i, seq_len):
        seq_len = min(seq_len, len(self.data) - 1 - i)
        return self.data[i:i+seq_len], self.data[i+1:i+1+seq_len].contiguous().view(-1)

def dropout_mask(x, sz, p):
    "Returns a dropout mask of the same type as x, size sz, with probability p to cancel an element."
    return x.new(*sz).bernoulli_(1-p).div_(1-p)

class RNNDropout(nn.Module):
    def __init__(self, p=0.5):
        super().__init__()
        self.p=p

    def forward(self, x):
        if not self.training or self.p == 0.: return x
        m = dropout_mask(x.data, (1, x.size(1), x.size(2)), self.p)
        return x * m

import warnings

class WeightDropout(nn.Module):
    "A module that warps another layer in which some weights will be replaced by 0 during training."

    def __init__(self, module, weight_p, layer_names=['weight_hh_l0']):
        super().__init__()
        self.module,self.weight_p,self.layer_names = module,weight_p,layer_names
        for layer in self.layer_names:
            #Makes a copy of the weights of the selected layers.
            w = getattr(self.module, layer)
            self.register_parameter(f'{layer}_raw', nn.Parameter(w.data))

    def _setweights(self):
        for layer in self.layer_names:
            raw_w = getattr(self, f'{layer}_raw')
            self.module._parameters[layer] = F.dropout(raw_w, p=self.weight_p, training=self.training)

    def forward(self, *args):
        self._setweights()
        with warnings.catch_warnings():
            #To avoid the warning that comes because the weights aren't flattened.
            warnings.simplefilter("ignore")
            return self.module.forward(*args)

    def reset(self):
        if hasattr(self.module, 'reset'): self.module.reset()

class EmbeddingDropout(nn.Module):

    "Applies dropout in the embedding layer by zeroing out some elements of the embedding vector."
    def __init__(self, emb, embed_p):
        super().__init__()
        self.emb,self.embed_p = emb,embed_p
        self.pad_idx = self.emb.padding_idx
        if self.pad_idx is None: self.pad_idx = -1

    def forward(self, words, scale=None):
        if self.training and self.embed_p != 0:
            size = (self.emb.weight.size(0),1)
            mask = dropout_mask(self.emb.weight.data, size, self.embed_p)
            masked_embed = self.emb.weight * mask
        else: masked_embed = self.emb.weight
        if scale: masked_embed.mul_(scale)
        return F.embedding(words, masked_embed, self.pad_idx, self.emb.max_norm,
                           self.emb.norm_type, self.emb.scale_grad_by_freq, self.emb.sparse)

def repackage_var(h):
    "Detaches h from its history."
    return h.detach() if type(h) == torch.Tensor else tuple(repackage_var(v) for v in h)

class RNNCore(nn.Module):
    "AWD-LSTM/QRNN inspired by https://arxiv.org/abs/1708.02182"

    initrange=0.1

    def __init__(self, vocab_sz, emb_sz, n_hid, n_layers, pad_token, bidir=False,
                 hidden_p=0.2, input_p=0.6, embed_p=0.1, weight_p=0.5, qrnn=False):

        super().__init__()
        self.bs,self.qrnn,self.ndir = 1, qrnn,(2 if bidir else 1)
        self.emb_sz,self.n_hid,self.n_layers = emb_sz,n_hid,n_layers
        self.encoder = nn.Embedding(vocab_sz, emb_sz, padding_idx=pad_token)
        self.encoder_dp = EmbeddingDropout(self.encoder, embed_p)
        if self.qrnn:
            #Using QRNN requires cupy: https://github.com/cupy/cupy
            from qrnn import QRNNLayer
            self.rnns = [QRNNLayer(emb_sz if l == 0 else n_hid, (n_hid if l != n_layers - 1 else emb_sz)//self.ndir,
                                   save_prev_x=True, zoneout=0, window=2 if l == 0 else 1, output_gate=True,
                                   use_cuda=torch.cuda.is_available()) for l in range(n_layers)]
            if weight_p != 0.:
                for rnn in self.rnns:
                    rnn.linear = WeightDropout(rnn.linear, weight_p, layer_names=['weight'])
        else:
            self.rnns = [nn.LSTM(emb_sz if l == 0 else n_hid, (n_hid if l != n_layers - 1 else emb_sz)//self.ndir,
                1, bidirectional=bidir) for l in range(n_layers)]
            if weight_p != 0.: self.rnns = [WeightDropout(rnn, weight_p) for rnn in self.rnns]
        self.rnns = torch.nn.ModuleList(self.rnns)
        self.encoder.weight.data.uniform_(-self.initrange, self.initrange)
        self.input_dp = RNNDropout(input_p)
        self.hidden_dps = nn.ModuleList([RNNDropout(hidden_p) for l in range(n_layers)])

    def forward(self, input):
        sl,bs = input.size()
        if bs!=self.bs:
            self.bs=bs
            self.reset()
        raw_output = self.input_dp(self.encoder_dp(input))
        new_hidden,raw_outputs,outputs = [],[],[]
        for l, (rnn,hid_dp) in enumerate(zip(self.rnns, self.hidden_dps)):
            raw_output, new_h = rnn(raw_output, self.hidden[l])
            new_hidden.append(new_h)
            raw_outputs.append(raw_output)
            if l != self.n_layers - 1: raw_output = hid_dp(raw_output)
            outputs.append(raw_output)
        self.hidden = repackage_var(new_hidden)
        return raw_outputs, outputs

    def one_hidden(self, l):
        nh = (self.n_hid if l != self.n_layers - 1 else self.emb_sz)//self.ndir
        return self.weights.new(self.ndir, self.bs, nh).zero_()

    def reset(self):
        [r.reset() for r in self.rnns if hasattr(r, 'reset')]
        self.weights = next(self.parameters()).data
        if self.qrnn: self.hidden = [self.one_hidden(l) for l in range(self.n_layers)]
        else: self.hidden = [(self.one_hidden(l), self.one_hidden(l)) for l in range(self.n_layers)]

class LinearDecoder(nn.Module):
    "To go on top of a RNN_Core module"

    initrange=0.1

    def __init__(self, n_out, n_hid, output_p, tie_encoder=None, bias=True):
        super().__init__()
        self.decoder = nn.Linear(n_hid, n_out, bias=bias)
        self.decoder.weight.data.uniform_(-self.initrange, self.initrange)
        self.output_dp = RNNDropout(output_p)
        if bias: self.decoder.bias.data.zero_()
        if tie_encoder: self.decoder.weight = tie_encoder.weight

    def forward(self, input):
        raw_outputs, outputs = input
        output = self.output_dp(outputs[-1])
        decoded = self.decoder(output.view(output.size(0)*output.size(1), output.size(2)))
        return decoded, raw_outputs, outputs

class SequentialRNN(nn.Sequential):
    def reset(self):
        for c in self.children():
            if hasattr(c, 'reset'): c.reset()

def get_language_model(vocab_sz, emb_sz, n_hid, n_layers, pad_token, tie_weights=True, qrnn=False, bias=True,
                 output_p=0.4, hidden_p=0.2, input_p=0.6, embed_p=0.1, weight_p=0.5):
    "To create a full AWD-LSTM"
    rnn_enc = RNNCore(vocab_sz, emb_sz, n_hid=n_hid, n_layers=n_layers, pad_token=pad_token, qrnn=qrnn,
                 hidden_p=hidden_p, input_p=input_p, embed_p=embed_p, weight_p=weight_p)
    enc = rnn_enc.encoder if tie_weights else None
    return SequentialRNN(rnn_enc, LinearDecoder(vocab_sz, emb_sz, output_p, tie_encoder=enc, bias=bias))

@dataclass
class GradientClipping(Callback):
    learn:Learner
    clip:float

    def on_backward_end(self, **kwargs):
        if self.clip:  nn.utils.clip_grad_norm_(self.learn.model.parameters(), self.clip)

@dataclass
class RNNTrainer(Callback):
    learn:Learner
    bptt:int
    alpha:float=0.
    beta:float=0.
    adjust:bool=True

    def on_loss_begin(self, last_output, **kwargs):
        #Save the extra outputs for later and only returns the true output.
        self.raw_out,self.out = last_output[1],last_output[2]
        return last_output[0]

    def on_backward_begin(self, last_loss, last_input, last_output, **kwargs):
        #Adjusts the lr to the bptt selected
        if self.adjust: self.learn.opt.lr *= last_input.size(0) / self.bptt
        #AR and TAR
        if self.alpha != 0.:  last_loss += (self.alpha * self.out[-1].pow(2).mean()).sum()
        if self.beta != 0.:
            h = self.raw_out[-1]
            if len(h)>1: last_loss += (self.beta * (h[1:] - h[:-1]).pow(2).mean()).sum()
        return last_loss