import socket
import argparse

import rvap.common.util as util

def process_client(server_ip='127.0.0.1', port_num=50008):
    
    STEP_STDOUT = 20
    
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock_server:
        
        sock_server.connect((server_ip, port_num))
        num_step = 0
        
        flag_now = False
        flag_future = False
        
        while True:
            try:
                
                data_size = sock_server.recv(4)
                size = int.from_bytes(data_size, 'little')
                
                data = sock_server.recv(size)
                while len(data) < size:
                    data += sock_server.recv(size - len(data))
                
                vap_result = util.conv_bytearray_2_vapresult(data)
                p_now = vap_result['p_now']
                p_future = vap_result['p_future']
                
                num_step += 1
                
                if num_step % STEP_STDOUT == 0:
                    print('-----------------------')
                    #print('t:', vap_result['t'])
                    #print('x1:', vap_result['x1'][:10])
                    #print('x2:', vap_result['x2'][:10])
                    #print('p_now:', vap_result['p_now'])
                    #print('p_future:', vap_result['p_future'])
                    print('p_now (x1, x2):', p_now)
                    print('p_future (x1, x2):', p_future)
                    
                    if p_now[0] > p_now[1]:
                        flag_now = False
                    else:
                        if flag_now == False:
                            print('VAP short-term: TRUE')
                        flag_now = True
                        
                    if p_future[0] > p_future[1]:
                        flag_future = False
                    else:
                        if flag_future == False:
                            print('VAP long-term: TRUE')
                        flag_future = True
                    
                    num_step = 0

            except Exception as e:
                print('Disconnected from the server')
                print(e)
                break

if __name__ == "__main__":

    # Argparse
    parser = argparse.ArgumentParser()
    #parser.add_argument("--checkpoint_dict", type=str, default='./model/state_dict_20hz.pt')
    parser.add_argument("--server_ip", type=str, default='127.0.0.1')
    parser.add_argument("--port_num", type=int, default=50008)
    args = parser.parse_args()
    
    process_client(args.server_ip, args.port_num)