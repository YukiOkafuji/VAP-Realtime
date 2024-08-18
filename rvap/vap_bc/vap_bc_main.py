import torch
import torch.nn as nn
import torch.nn.functional as F
from torch import Tensor

from dataclasses import dataclass, field
from typing import Dict, Tuple, List, Optional

from rvap.vap_bc.encoder import EncoderCPC
from rvap.vap_bc.modules import GPT, GPTStereo
from rvap.vap_bc.objective import ObjectiveVAP

# from wav import WavLoadForVAP
import time
import threading

import numpy as np

import socket
import copy
import argparse

from os import environ
environ["CUBLAS_WORKSPACE_CONFIG"] = ":4096:8"

import rvap.common.util as util

torch.backends.cudnn.deterministic = True
torch.use_deterministic_algorithms(mode=True)

torch.manual_seed(0)

BIN_TIMES: list = [0.2, 0.4, 0.6, 0.8]

@dataclass
class VapConfig:
    sample_rate: int = 16000
    frame_hz: int = 10
    bin_times: List[float] = field(default_factory=lambda: BIN_TIMES)

    # Encoder (training flag)
    encoder_type: str = "cpc"
    wav2vec_type: str = "mms"
    hubert_model: str = "hubert_jp"
    freeze_encoder: int = 1  # stupid but works (--vap_freeze_encoder 1)
    load_pretrained: int = 1  # stupid but works (--vap_load_pretrained 1)
    only_feature_extraction: int = 0

    # GPT
    dim: int = 256
    channel_layers: int = 1
    cross_layers: int = 3
    num_heads: int = 4
    dropout: float = 0.1
    context_limit: int = -1

    context_limit_cpc_sec: float = -1

    # Added Multi-task
    lid_classify: int = 0   # 1...last layer, 2...middle layer
    lid_classify_num_class: int = 3
    lid_classify_adversarial: int = 0
    lang_cond: int = 0

    @staticmethod
    def add_argparse_args(parser, fields_added=[]):
        for k, v in VapConfig.__dataclass_fields__.items():
            if k == "bin_times":
                parser.add_argument(
                    f"--vap_{k}", nargs="+", type=float, default=v.default_factory()
                )
            else:
                parser.add_argument(f"--vap_{k}", type=v.type, default=v.default)
            fields_added.append(k)
        return parser, fields_added

    @staticmethod
    def args_to_conf(args):
        return VapConfig(
            **{
                k.replace("vap_", ""): v
                for k, v in vars(args).items()
                if k.startswith("vap_")
            }
        )

class VapGPT(nn.Module):
    
    def __init__(self, conf: Optional[VapConfig] = None):
        super().__init__()
        if conf is None:
            conf = VapConfig()
        self.conf = conf
        self.sample_rate = conf.sample_rate
        self.frame_hz = conf.frame_hz

        self.temp_elapse_time = []

        # Single channel
        self.ar_channel = GPT(
            dim=conf.dim,
            dff_k=3,
            num_layers=conf.channel_layers,
            num_heads=conf.num_heads,
            dropout=conf.dropout,
            context_limit=conf.context_limit,
        )

        # Cross channel
        self.ar = GPTStereo(
            dim=conf.dim,
            dff_k=3,
            num_layers=conf.cross_layers,
            num_heads=conf.num_heads,
            dropout=conf.dropout,
            context_limit=conf.context_limit,
        )

        self.objective = ObjectiveVAP(bin_times=conf.bin_times, frame_hz=conf.frame_hz)

        # Outputs
        # Voice activity objective -> x1, x2 -> logits ->  BCE
        self.va_classifier = nn.Linear(conf.dim, 1)
        
        if self.conf.lid_classify == 1:
            self.lid_classifier = nn.Linear(conf.dim, conf.lid_classify_num_class)
        
        elif self.conf.lid_classify == 2:
            self.lid_classifier_middle = nn.Linear(conf.dim*2, conf.lid_classify_num_class)
        
        if self.conf.lang_cond == 1:
            self.lang_condition = nn.Linear(conf.lid_classify_num_class, conf.dim)
        
        self.vap_head = nn.Linear(conf.dim, self.objective.n_classes)

        # For Backchannel
        self.bc_head_react = nn.Linear(conf.dim, 1)
        self.bc_head_emo = nn.Linear(conf.dim, 1)

    def load_encoder(self, cpc_model):
        
        # Audio Encoder
        self.encoder1 = EncoderCPC(
            load_pretrained=True if self.conf.load_pretrained == 1 else False,
            freeze=self.conf.freeze_encoder,
            cpc_model=cpc_model
        )
        self.encoder1 = self.encoder1.eval()
        
        self.encoder2 = EncoderCPC(
            load_pretrained=True if self.conf.load_pretrained == 1 else False,
            freeze=self.conf.freeze_encoder,
            cpc_model=cpc_model
        )

        self.encoder2 = self.encoder2.eval()
        
        if self.conf.freeze_encoder == 1:
            print('freeze encoder')
            self.encoder1.freeze()
            self.encoder2.freeze()

    @property
    def horizon_time(self):
        return self.objective.horizon_time

    def encode_audio(self, audio1: torch.Tensor, audio2: torch.Tensor) -> Tuple[Tensor, Tensor]:
        
        x1 = self.encoder1(audio1)  # speaker 1
        x2 = self.encoder2(audio2)  # speaker 2

        return x1, x2

    def vad_loss(self, vad_output, vad):
        return F.binary_cross_entropy_with_logits(vad_output, vad)


class VAPRealTime():
    
    BINS_P_NOW = [0, 1]
    BINS_PFUTURE = [2, 3]
    
    CALC_PROCESS_TIME_INTERVAL = 100
        
    def __init__(self, vap_model, cpc_model, device):
        
        conf = VapConfig()
        self.vap = VapGPT(conf)

        self.device = device

        sd = torch.load(vap_model, map_location=torch.device('cpu'))
        self.vap.load_encoder(cpc_model=cpc_model)
        self.vap.load_state_dict(sd, strict=False)
        
        # The downsampling parameters are not loaded by "load_state_dict"
        self.vap.encoder1.downsample[1].weight = nn.Parameter(sd['encoder.downsample.1.weight'])
        self.vap.encoder1.downsample[1].bias = nn.Parameter(sd['encoder.downsample.1.bias'])
        self.vap.encoder1.downsample[2].ln.weight = nn.Parameter(sd['encoder.downsample.2.ln.weight'])
        self.vap.encoder1.downsample[2].ln.bias = nn.Parameter(sd['encoder.downsample.2.ln.bias'])
        
        self.vap.encoder2.downsample[1].weight = nn.Parameter(sd['encoder.downsample.1.weight'])
        self.vap.encoder2.downsample[1].bias = nn.Parameter(sd['encoder.downsample.1.bias'])
        self.vap.encoder2.downsample[2].ln.weight = nn.Parameter(sd['encoder.downsample.2.ln.weight'])
        self.vap.encoder2.downsample[2].ln.bias = nn.Parameter(sd['encoder.downsample.2.ln.bias'])

        self.vap.to(self.device)
        self.vap = self.vap.eval()

        # Context length of the audio embeddings (depends on frame rate)
        self.AUDIO_CONTEXT_LIM = 50
        
        self.audio_contenxt_lim_sec = 5
        self.frame_hz = 10
        self.sampling_rate = 16000
        self.frame_contxt_padding = 320 # Independe from frame size
        
        # Frame size
        # 10Hz -> 320 + 1600 samples
        # 20Hz -> 320 + 800 samples
        # 50Hz -> 320 + 320 samples
        self.audio_frame_size = self.sampling_rate // self.frame_hz + self.frame_contxt_padding
        
        self.current_x1_audio = []
        self.current_x2_audio = []
        
        self.result_p_bc_react = 0.
        self.result_p_bc_emo = 0.
        self.result_last_time = -1

        self.process_time_abs = -1

        self.e1_context = []
        self.e2_context = []
        
        self.list_process_time_context = []
    
    def process_vap(self, x1, x2):
        
        # Frame size
        # 10Hz -> 320 + 1600 samples
        # 20Hz -> 320 + 800 samples
        # 50Hz -> 320 + 320 samples
        
        time_start = time.time()
        
        # Save the current audio data
        self.current_x1_audio = x1[self.frame_contxt_padding:]
        self.current_x2_audio = x2[self.frame_contxt_padding:]
        
        with torch.no_grad():
            
            x1_ = torch.tensor([[x1]], dtype=torch.float32, device=self.device)
            x2_ = torch.tensor([[x2]], dtype=torch.float32, device=self.device)

            e1, e2 = self.vap.encode_audio(x1_, x2_)
            
            self.e1_context.append(e1)
            self.e2_context.append(e2)
            
            if len(self.e1_context) > self.AUDIO_CONTEXT_LIM:
                self.e1_context = self.e1_context[-self.AUDIO_CONTEXT_LIM:]
            if len(self.e2_context) > self.AUDIO_CONTEXT_LIM:
                self.e2_context = self.e2_context[-self.AUDIO_CONTEXT_LIM:]
            
            x1_ = torch.cat(self.e1_context, dim=1).to(self.device)
            x2_ = torch.cat(self.e2_context, dim=1).to(self.device)

            o1 = self.vap.ar_channel(x1_, attention=False)  # ["x"]
            o2 = self.vap.ar_channel(x2_, attention=False)  # ["x"]
            out = self.vap.ar(o1["x"], o2["x"], attention=False)

            # Outputs
            #v1 = self.va_classifier(out["x1"])
            #v2 = self.va_classifier(out["x2"])
            #vad = torch.cat((v1, v2), dim=-1)
            bc_react = self.vap.bc_head_react(out["x"])
            bc_emo = self.vap.bc_head_emo(out["x"])
            #logits = self.vap_head(out["x"])

            p_bc_react = bc_react.sigmoid()
            p_bc_emo = bc_emo.sigmoid()


            
            # o1 = self.vap.ar_channel(x1_, attention=False)
            # o2 = self.vap.ar_channel(x2_, attention=False)
            # out = self.vap.ar(o1["x"], o2["x"], attention=False)
            
            # # Outputs
            # logits = self.vap.vap_head(out["x"])
            # probs = logits.softmax(dim=-1)
            
            # p_now = self.vap.objective.probs_next_speaker_aggregate(
            #     probs,
            #     from_bin=self.BINS_P_NOW[0],
            #     to_bin=self.BINS_P_NOW[-1]
            # )
            
            # p_future = self.vap.objective.probs_next_speaker_aggregate(
            #     probs,
            #     from_bin=self.BINS_PFUTURE[0],
            #     to_bin=self.BINS_PFUTURE[1]
            # )
            
            # Get back to the CPU
            p_bc_react = p_bc_react.to('cpu')
            p_bc_emo = p_bc_emo.to('cpu')
            
            self.result_p_bc_react = p_bc_react.tolist()[0][-1]
            self.result_p_bc_emo = p_bc_emo.tolist()[0][-1]
            self.result_last_time = time.time()
            
            time_process = time.time() - time_start
            
            # Calculate the average encoding time
            self.list_process_time_context.append(time_process)
            
            if len(self.list_process_time_context) > self.CALC_PROCESS_TIME_INTERVAL:
                ave_proc_time = np.average(self.list_process_time_context)
                print('[VAP] Average processing time: %.5f [sec]' % ave_proc_time)
                self.list_process_time_context = []
            
            self.process_time_abs = time.time()
                    

def proc_serv_out(list_socket_out, port_number=50008):
    
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        
        s.bind(('127.0.0.1', port_number))
        s.listen(1)

        while True:
            conn, addr = s.accept()
            print('[OUT] Connected by', addr)
            conn.setblocking(False)
            conn.settimeout(0)
            list_socket_out.append(conn)
            
            print('[OUT] Current client num = %d' % len(list_socket_out))

def proc_serv_in(port_number, vap):
    
    # Frame size
    # 10Hz -> 320 + 1600 samples
    # 20Hz -> 320 + 800 samples
    # 50Hz -> 320 + 320 samples

    FRAME_SIZE_INPUT = 160
    FRAME_SIZE_VAP = 1600
    FRAME_SAVE_LAST = 320
    
    while True:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(('127.0.0.1', port_number))
            s.listen(1)
        
            print('[IN] Waiting for connection of audio input...')
            conn, addr = s.accept()
            print('[IN] Connected by', addr)
            
            current_x1 = np.zeros(FRAME_SAVE_LAST)
            current_x2 = np.zeros(FRAME_SAVE_LAST)
            
            while True:
                
                # Float (4 byte) x 2 persons x 160 samples (0.01 sec)
                size_recv = 8 * 2 * FRAME_SIZE_INPUT
                data = conn.recv(size_recv)

                # Continue to receive data until the size of the data is 
                # equal to the size of the data to be received
                if len(data) < size_recv:
                    while True:
                        data_ = conn.recv(size_recv)
                        if len(data_) == 0:
                            break
                        data += data_
                        if len(data) == size_recv:
                            break
                
                if len(data) == 0:
                    break
                
                x1, x2 = util.conv_bytearray_2_2floatarray(data)
                
                current_x1 = np.concatenate([current_x1, x1])
                current_x2 = np.concatenate([current_x2, x2])
                
                # Continue to receive data until the size of the data is
                # less that the size of the VAP frame
                if len(current_x1) < FRAME_SIZE_VAP + FRAME_SAVE_LAST:
                    continue
                
                vap.process_vap(current_x1, current_x2)
                
                # Save the last 320 samples
                current_x1 = current_x1[-FRAME_SAVE_LAST:]
                current_x2 = current_x2[-FRAME_SAVE_LAST:]
                
        except Exception as e:
            print('[IN] Disconnected by', addr)
            print(e)
            continue

def proc_serv_out_dist(list_socket_out, vap):
    
    previous_time = vap.process_time_abs
    
    while True:
        
        if previous_time == vap.process_time_abs:
            time.sleep(1E-5)
            continue
        
        previous_time = vap.process_time_abs
        
        t = copy.copy(vap.result_last_time)
        x1 = copy.copy(vap.current_x1_audio)
        x2 = copy.copy(vap.current_x2_audio)
        p_bc_react = copy.copy(vap.result_p_bc_react)
        p_bc_emo = copy.copy(vap.result_p_bc_emo)
        
        vap_result = {
            "t": t,
            "x1": x1, "x2": x2,
            "p_bc_react": p_bc_react, "p_bc_emo": p_bc_emo
        }
        
        data_sent = util.conv_vapresult_2_bytearray_bc(vap_result)
        sent_size = len(data_sent)
        data_sent_all = sent_size.to_bytes(4, 'little') + data_sent
        
        for conn in list_socket_out:
            try:
                if conn.fileno() != -1:
                    conn.sendall(data_sent_all)
            except:
                print('[OUT] Disconnected by', conn.getpeername())
                list_socket_out.remove(conn)
                continue


if __name__ == "__main__":
    
    # Argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--vap_model", type=str, default='../../asset/vap_bc/vap_bc_multi_state_dict_10hz_jpn.pt')
    parser.add_argument("--cpc_model", type=str, default='../../asset/cpc/60k_epoch4-d0f474de.pt')
    parser.add_argument("--port_num_in", type=int, default=50007)
    parser.add_argument("--port_num_out", type=int, default=50008)
    parser.add_argument("--gpu", action='store_true')
    args = parser.parse_args()
    
    #
    # GPU Usage
    #
    device = torch.device('cpu')
    if args.gpu:
        if torch.cuda.is_available():
            device = torch.device('cuda')
    print('Device: ', device)
    
    wait_input = True

    vap = VAPRealTime(args.vap_model, args.cpc_model, device)
    
    list_socket_out = []

    # Start the server to receive the connection for sending the VAP results
    t_server_out_connect = threading.Thread(target=proc_serv_out, args=(list_socket_out, args.port_num_out,))
    t_server_out_connect.setDaemon(True)
    t_server_out_connect.start()

    # Check the connection and processing of the VAP and then send the VAP results
    t_server_out_distribute = threading.Thread(target=proc_serv_out_dist, args=(list_socket_out, vap))
    t_server_out_distribute.setDaemon(True)
    t_server_out_distribute.start()

    # This process must be run in the main thread
    proc_serv_in(args.port_num_in, vap)
    
    # Continue unitl pressind "Ctrl+c"
    print('Press Ctrl+c to stop')
    while True:
        time.sleep(1E-5)
        pass
    
   