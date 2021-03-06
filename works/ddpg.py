import torch
import torch.optim as optim
import torch.nn.functional as F
import numpy as np
import datetime
import os
import copy
import timeit
from torch.utils.tensorboard import SummaryWriter
from actornetwork import MuNet
from criticnetwork import QNet
from gym_torcs import TorcsEnv
from replaybuffer import ReplayBuffer
from ou import OrnsteinUhlenbeckNoise as OUN

state_dim = 29
action_dim = 2
max_episode = 200
max_step = 2000
train_start_size = 1500
iteration = 0 # global step for record

EXPLORE      = 70000
lr_mu        = 0.001
lr_q         = 0.0001
gamma        = 0.99
batch_size   = 64
tau          = 0.001


def main():
    """main method
    
    log runtime and print it at the end
    """
    s_time = timeit.default_timer()     
    global iteration
    env = TorcsEnv(vision=False, throttle=True, gear_change=False)
    memory = ReplayBuffer()
    epsilon = 1
    train_indicator = True
    modelPATH = os.path.join('.',"models",'E0011.pt')

    q,q_target = QNet(state_dim,action_dim),QNet(state_dim,action_dim)
    q_target.load_state_dict(q.state_dict())
    mu, mu_target = MuNet(state_dim), MuNet(state_dim)
    mu_target.load_state_dict(mu.state_dict())
    steer_noise = OUN(np.zeros(1),theta = 0.6)
    accel_noise = OUN(np.zeros(1),theta = 0.6)
    mu_optimizer = optim.Adam(mu.parameters(), lr=lr_mu)
    q_optimizer  = optim.Adam(q.parameters(), lr=lr_q)

    #tensorboard writer
    current_time = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    log_dir = os.path.join("logs", "ddpg_torch", current_time+'E0011t')
    writer = SummaryWriter(log_dir)
    samplestate = torch.rand(1,29)
    sampleaction = torch.rand(1,2)

    #writer.add_graph(mu,samplestate)
    writer.add_graph(q,(samplestate,sampleaction))
    writer.close

    if train_indicator ==False:
        mu = torch.load(modelPATH)
        mu.eval()
        ob = env.reset()
        score = 0
        for n_step in range(100000):
            s_t = np.hstack((ob.angle, ob.track,ob.trackPos,ob.speedX, ob.speedY,  ob.speedZ, ob.wheelSpinVel/100.0, ob.rpm))
            a_t = mu(torch.from_numpy(s_t.reshape(1,-1)).float()).detach().numpy()
            ob,r_t,done,_ = env.step(a_t[0])
            score += r_t
            if done:
                print("score:",score)
                break
        env.end()
        return 0

    for n_epi in range(max_episode):
        print("Episode : " + str(n_epi) + " Replay Buffer " + str(memory.size()))
        if np.mod(n_epi, 3) == 0:
            ob = env.reset(relaunch=True)   #relaunch TORCS every 3 episode because of the memory leak error
        else:
            ob = env.reset()
        a_t = np.zeros([1,action_dim])
        s_t = np.hstack((ob.angle, ob.track,ob.trackPos,ob.speedX, ob.speedY,  ob.speedZ, ob.wheelSpinVel/100.0, ob.rpm))
        score = 0
        q_value_writer(q, mu, s_t, writer, 'Episode Start Q value')
        q_value_writer(q_target, mu_target, s_t, writer, 'Episode Start target Q value')
        #t_start = timeit.default_timer()
        for n_step in range(max_step):
            #epsilon -= 1.0/EXPLORE
            a_origin = mu(torch.from_numpy(s_t.reshape(1,-1)).float())
            if train_indicator == True:#add noise for train
                # sn = max(epsilon,0)*steer_noise()
                sn = steer_noise()
                # an = max(epsilon,0)*accel_noise()
                an = accel_noise()
                a_s = a_origin.detach().numpy()[0][0] + sn
                a_t[0][0] = np.clip(a_s,-1,1) # fit in steer arange
                a_a = a_origin.detach().numpy()[0][1] + an
                a_t[0][1] = np.clip(a_a,0,1) # fit in accel arange
                #record noise movement
                if iteration%10==0:
                    writer.add_scalar('Steer noise', sn, iteration)
                    writer.add_scalar('Accel_noise', an, iteration)
            else:
                a_t = a_origin.detatch().numpy()
            ob,r_t,done,_ = env.step(a_t[0])
            score += r_t

            s_t1 = np.hstack((ob.angle, ob.track, ob.trackPos, ob.speedX, ob.speedY, ob.speedZ, ob.wheelSpinVel/100.0, ob.rpm))
            memory.put((s_t,a_t[0],r_t,s_t1,done))
            s_temp = copy.deepcopy(s_t) # for end q value log
            s_t = s_t1

            if train_indicator and memory.size()>train_start_size:
                train(mu, mu_target, q, q_target, memory, q_optimizer, mu_optimizer,writer)
                soft_update(mu, mu_target)
                soft_update(q,  q_target)
            
            iteration+=1

            if done:
                q_value_writer(q,mu,s_temp,writer,'Episode End Q value')
                q_value_writer(q_target,mu_target,s_temp,writer,'Episode End target Q value')
                break
        #t_end = timeit.default_timer()
        
        print("TOTAL REWARD @ " + str(n_epi) +"-th Episode  : Reward " + str(score))
        print("Total Step: " + str(n_step))
        print("")
        #print('{}steps, {} time spent'.format(i,t_end-t_start))
    
    torch.save(mu,modelPATH)
    
    env.end()
    
    e_time = timeit.default_timer()
    print("Total step {} and time spent {}".format(iteration, e_time-s_time))
    # s,a,r,sp,d = memory.sample(1)
    # print('s: ',s)
    # print('a: ',a)
    # print('r: ',r)
    # print('sp: ', sp)
    # print('d: ',d)

def train(mu, mu_target, q, q_target, memory, q_optimizer, mu_optimizer, writer):
    global iteration
    s,a,r,s_prime,done_mask  = memory.sample(batch_size)
    # target = r if done
    target = r + np.logical_not(done_mask) *gamma*q_target(s_prime, mu_target(s_prime))
    q_loss = F.smooth_l1_loss(q(s,a), target.detach())
    q_optimizer.zero_grad()     #backward()가 가중치를 덮어씌우지 않고, 누적하기 때문에 초기화 한다.
    q_loss.backward()
    q_optimizer.step()
    mu_loss = -q(s,mu(s)).mean() # That's all for the policy loss.
    mu_optimizer.zero_grad()
    mu_loss.backward()
    mu_optimizer.step()

    if iteration%10==0: # tensorboard record
        writer.add_scalar('reward',r.mean().item(),iteration)
        writer.add_scalar('Q_loss',q_loss.item(),iteration)
        writer.add_scalar('Mu_loss',mu_loss.item(),iteration)

def soft_update(net, net_target):
    for param_target, param in zip(net_target.parameters(), net.parameters()):
        param_target.data.copy_(param_target.data * (1.0 - tau) + param.data * tau)


def q_value_writer(q_net,mu_net,state,writer,parameter_name):
    global iteration
    state_tensor = torch.tensor(state.reshape(1,-1),dtype=torch.float)
    action_tensor = mu_net(state_tensor)
    q = q_net(state_tensor,action_tensor)
    writer.add_scalar(parameter_name, q.item(),iteration)
if __name__ == '__main__':
    main()
