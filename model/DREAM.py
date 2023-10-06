import torch
import torch.nn as nn
from torch.nn import functional as F
import torch.distributions as dist
from TorchCRF import CRF
from model.base import Decoder_ResNet, Encoder_ResNet, p_decoder, aux_layer, Augmentation
from model.loss import SupervisedContrastiveLoss, Self_SupervisedContrastiveLoss
import random

class VAE(nn.Module):
    def __init__(self, config, n_domains, sampling_rate, augment=True):
        super(VAE, self).__init__()
        self.d_dim = n_domains
        params = config['hyper_params']
        self.zd_dim = params['zd_dim']
        self.zx_dim = 0
        self.zy_dim = params['zy_dim']
        self.y_dim = params['num_classes']
        self.seq_len = params['seq_len']
        
        self.sampling_rate = sampling_rate
            
        self.contrastive_loss = SupervisedContrastiveLoss()
        self.augment = augment
        if self.augment:
            self.augmentor = Augmentation(sampling_rate=sampling_rate)
            
        self.px = Decoder_ResNet(self.zd_dim, self.zx_dim, self.zy_dim, self.sampling_rate)
        self.pzd = p_decoder(self.d_dim, self.zd_dim)
        self.pzy = p_decoder(self.y_dim, self.zy_dim)

        self.qzd = Encoder_ResNet(self.zd_dim, self.sampling_rate)
        if self.zx_dim != 0:
            self.qzx = Encoder_ResNet(self.zx_dim, self.sampling_rate)
        self.qzy = Encoder_ResNet(self.zy_dim, self.sampling_rate)

        # auxiliary
        self.qd = aux_layer(self.zd_dim, self.d_dim)
        self.qy = aux_layer(self.zy_dim, self.y_dim)

        self.aux_loss_multiplier_y = params['aux_loss_y']
        self.aux_loss_multiplier_d = params['aux_loss_d']

        self.beta_x,self.beta_y,self.beta_d = params['beta_x'],params['beta_y'],params['beta_d']
        self.const_weight = params['const_weight']

    def forward(self, x, y, d):
        # Encode
        zd_q_loc, zd_q_scale = self.qzd(x)
        if self.zx_dim != 0:
            zx_q_loc, zx_q_scale = self.qzx(x)
        zy_q_loc, zy_q_scale = self.qzy(x)

        # Reparameterization trick
        qzd = dist.Normal(zd_q_loc, zd_q_scale)
        zd_q = qzd.rsample()
        if self.zx_dim != 0:
            qzx = dist.Normal(zx_q_loc, zx_q_scale)
            zx_q = qzx.rsample()
        else:
            qzx = None
            zx_q = None

        qzy = dist.Normal(zy_q_loc, zy_q_scale)
        zy_q = qzy.rsample()

        # Decode
        x_recon = self.px(zx=zx_q, zy=zy_q, zd=zd_q)
        zd_p_loc, zd_p_scale = self.pzd(d)
        if self.zx_dim != 0:
            zx_p_loc, zx_p_scale = torch.zeros(zd_p_loc.size()[0], self.zx_dim).cuda(),\
                                   torch.ones(zd_p_loc.size()[0], self.zx_dim).cuda()
        zy_p_loc, zy_p_scale = self.pzy(y)

        # Reparameterization trick
        pzd = dist.Normal(zd_p_loc, zd_p_scale)
        if self.zx_dim != 0:
            pzx = dist.Normal(zx_p_loc, zx_p_scale)
        else:
            pzx = None
        pzy = dist.Normal(zy_p_loc, zy_p_scale)

        # Auxiliary losses
        d_hat = self.qd(zd_q)
        y_hat = self.qy(zy_q)
        return x_recon, d_hat, y_hat, qzd, pzd, zd_q, qzx, pzx, zx_q, qzy, pzy, zy_q, zy_q_loc
        

    def get_losses(self, x, y, d):        
        VAE_losses, conts_losses = 0, 0
        d_target = d
        d_input = F.one_hot(d, num_classes=self.d_dim).float()
        for i in range(self.seq_len):
            x_sample = x[:, i].view(x.size(0),1, -1)  
            y_target = y[:, i]
            y_input = F.one_hot(y_target, num_classes= self.y_dim).float() 
            if self.augment:
                x_croped = self.augmentor.crop_resize(x_sample,random.uniform(0.25,0.75))
                x_croped  = torch.FloatTensor(x_croped).cuda()
                x_permuted = self.augmentor.permute(x_sample,random.randint(5,20))
                x_permuted  = torch.FloatTensor(x_permuted).cuda()
                x_set = [x_croped, x_permuted]
            else:
                x_set = [x_sample]
            
            for x_input in x_set:
                x_recon, d_hat, y_hat, qzd, pzd, zd_q, qzx, pzx, zx_q, qzy, pzy, zy_q, features = self.forward(x_input, y_input, d_input)
    
                CE_x = F.mse_loss(x_recon, x_input, reduction='sum')
                zd_p_minus_zd_q = torch.sum(pzd.log_prob(zd_q) - qzd.log_prob(zd_q))
                if self.zx_dim != 0:
                    KL_zx = torch.sum(pzx.log_prob(zx_q) - qzx.log_prob(zx_q))
                else:
                    KL_zx = 0
    
                zy_p_minus_zy_q = torch.sum(pzy.log_prob(zy_q) - qzy.log_prob(zy_q))
    
                CE_d = F.cross_entropy(d_hat, d_target, reduction='sum')
                CE_y = F.cross_entropy(y_hat, y_target, reduction='sum')
    
                VAE_losses += CE_x \
                   - self.beta_d * zd_p_minus_zd_q \
                   - self.beta_x * KL_zx \
                   - self.beta_y * zy_p_minus_zy_q \
                   + self.aux_loss_multiplier_d * CE_d \
                   + self.aux_loss_multiplier_y * CE_y
                
                conts_losses += self.contrastive_loss(features, y_target)*self.const_weight
            
            
        return (VAE_losses+conts_losses)/self.seq_len


    def get_features(self, x):
        batch_size = x.size(0)
        f_seq = []
        for i in range(self.seq_len):            
            x_input = x[:, i].view(batch_size, 1, -1)
            features, _ = self.qzy.forward(x_input)
            f_seq.append(features.view(batch_size,1, -1))

        out = torch.cat(f_seq, dim=1)
        return out # (batch_size,len, n_feat)   
    
    def predict(self, x):
        batch_size = x.size(0)
        f_seq = []
        with torch.no_grad():
            for i in range(self.seq_len):   
                x_input = x[:, i].view(batch_size, 1, -1)
                zy, _ = self.qzy.forward(x_input)
                alpha = F.softmax(self.qy(zy), dim=1)
                res, ind = torch.topk(alpha, 1)
                y = x_input.new_zeros(alpha.size())
                y = y.scatter_(1, ind, 1.0)
                f_seq.append(y.view(batch_size,1, -1))
                
            out = torch.cat(f_seq, dim=1)
            out = out.permute(0,2,1)           # (batch_size, n_class, len)      
        return out
    

    
class Transformer(nn.Module):
    def __init__(self, config, n_layer=4, n_classes=5, is_crf=True, is_transformer=True):
        super(Transformer, self).__init__()
        params = config['hyper_params']
        self.hidden_dim = params['zy_dim']
        self.batch_size = config["data_loader"]["args"]["batch_size"]
        self.dim_feedforward = params['dim_feedforward']
        self.n_layer = params['n_layers']
        self.is_crf = is_crf
        self.is_transformer = is_transformer
        
        if self.is_crf:
            self.mask = torch.ones((self.batch_size, params['seq_len'])).byte().cuda()
            self.crf = CRF(n_classes)

        self.encoder_layer = nn.TransformerEncoderLayer(d_model=self.hidden_dim, nhead=8, batch_first=True, dim_feedforward=self.dim_feedforward) 
        self.transformer_encoder = nn.TransformerEncoder(self.encoder_layer, num_layers=self.n_layer)
        self.fc = nn.Linear(self.hidden_dim, n_classes)
        
    def forward(self, x): #in: (batch, seq, feature) if batch_first=True
        #out:(batch_size, seq_len, feature).
        if self.is_transformer:
            x = self.transformer_encoder(x)
        x = self.fc(x)
        return x  # (N_batch, Length, Class)
        
    def get_loss(self, x, y): 
        x = self.forward(x)  # out: (N_batch, Length, Class)
        if self.is_crf:
            mask = self.mask[:len(y)]
            loss = self.crf.forward(x, y, mask)  # y: (batch_size, sequence_size), mask: (batch_size, sequence_size), out: (batch_size, sequence_size, num_labels)
            loss = -loss.mean()
        else:
            x = F.softmax(x, dim=2)  # (N_batch, Length, Class)
            x = x.permute(0,2,1)     # (N_batch, Class, Length)
            loss = F.cross_entropy(x, y) # input:(N, C, L), out:(N,L)
        return loss
    
    def predict(self, x):
        x = self.forward(x) # out: (N_batch, Length, Class)
        if self.is_crf:
            mask = self.mask[:len(x)]
            y = self.crf.viterbi_decode(x, mask)
        else:
            x = F.softmax(x, dim=2)
            x = x.permute(0,2,1) # (N_batch, Class, Length)
            y = x.data.max(1)[1].cpu()
        return y
