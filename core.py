import sys
import time
import config as conf
import torch
import random
import numpy as np
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from collections import Counter, deque
import logging
import time
import os

sys.path.insert(0, 'precompiled')
import r
from env import Env as CEnv

WORK_DIR, _ = os.path.split(os.path.abspath(__file__))
lt = time.localtime(time.time())
BEGIN = '{:0>2d}{:0>2d}_{:0>2d}{:0>2d}'.format(
    lt.tm_mon, lt.tm_mday, lt.tm_hour, lt.tm_min)


def fn():
    res = os.path.join(WORK_DIR, 'outs', '{}.log'.format(BEGIN))
    return res


logger = logging.getLogger('DDZ_RL')
logger.setLevel(logging.INFO)
logging.basicConfig(filename=fn(), filemode='w',
                    format='[%(asctime)s][%(name)s][%(levelname)s]:  %(message)s')

DEVICE = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")


class Env(CEnv):
    def __init__(self, debug=False, seed=None):
        if seed:
            super(Env, self).__init__(seed=seed)
        else:
            super(Env, self).__init__()
        self.taken = np.zeros((15,))
        self.left = np.array([17, 20, 17], dtype=np.int)
        self.debug = debug

    def reset(self):
        super(Env, self).reset()
        self.taken = np.zeros((15,))
        self.left = np.array([17, 20, 17])

    def _update(self, role, cards):
        self.left[role] -= len(cards)
        for card, count in Counter(cards - 3).items():
            self.taken[card] += count
        if self.debug:
            if role == 1:
                name = '地主'
            elif role == 0:
                name = '农民1'
            else:
                name = '农民2'
            logger.info('{} 出牌： {}，分别剩余： {}'.format(
                name, self.cards2str(cards), self.left))

    def step_manual(self, onehot_cards):
        role = self.get_role_ID() - 1
        arr_cards = self.onehot2arr(onehot_cards)
        cards = self.arr2cards(arr_cards)

        self._update(role, cards)
        return super(Env, self).step_manual(cards)

    def step_auto(self):
        role = self.get_role_ID() - 1
        cards, r, _ = super(Env, self).step_auto()
        self._update(role, cards)
        return cards, r, _

    @property
    def face(self):
        """
        :return:  2 * 15 * 4 的数组，作为当前状态
        """
        handcards = self.cards2arr(self.get_curr_handcards())
        face = [handcards, self.taken]
        return torch.tensor(self.batch_arr2onehot(face), dtype=torch.float).to(DEVICE)

    @property
    def valid_actions(self):
        """
        :return:  batch_size * 15 * 4 的可行动作集合
        """
        handcards = self.cards2arr(self.get_curr_handcards())
        last_two = self.get_last_two_cards()
        if last_two[0]:
            last = last_two[0]
        elif last_two[1]:
            last = last_two[1]
        else:
            last = []
        last = self.cards2arr(last)
        actions = r.get_moves(handcards, last)
        return torch.tensor(self.batch_arr2onehot(actions), dtype=torch.float).to(DEVICE)

    @classmethod
    def arr2cards(cls, arr):
        """
        :param arr: 15 * 4
        :return: ['A','A','A', '3', '3'] 用 [3,3,14,14,14]表示
            [3,4,5,6,7,8,9,10, J, Q, K, A, 2,BJ,CJ]
            [3,4,5,6,7,8,9,10,11,12,13,14,15,16,17]
        """
        res = []
        for idx in range(15):
            for _ in range(arr[idx]):
                res.append(idx + 3)
        return np.array(res, dtype=np.int)

    @classmethod
    def cards2arr(cls, cards):
        arr = np.zeros((15,), dtype=np.int)
        for card in cards:
            arr[card - 3] += 1
        return arr

    @classmethod
    def batch_arr2onehot(cls, batch_arr):
        res = np.zeros((len(batch_arr), 15, 4), dtype=np.int)
        for idx, arr in enumerate(batch_arr):
            for card_idx, count in enumerate(arr):
                if count > 0:
                    res[idx][card_idx][:int(count)] = 1
        return res

    @classmethod
    def onehot2arr(cls, onehot_cards):
        """
        :param onehot_cards: 15 * 4
        :return: (15,)
        """
        res = np.zeros((15,), dtype=np.int)
        for idx, onehot in enumerate(onehot_cards):
            res[idx] = sum(onehot)
        return res

    def cards2str(self, cards):
        res = [conf.DICT[i] for i in cards]
        return res


class Net(nn.Module):
    def __init__(self):
        # input shape: 3 * 15 * 4
        super(Net, self).__init__()
        self.conv1 = nn.Conv2d(3, 64, (1, 1), (1, 4))
        self.conv2 = nn.Conv2d(3, 64, (1, 2), (1, 4))
        self.conv3 = nn.Conv2d(3, 64, (1, 3), (1, 4))
        self.conv4 = nn.Conv2d(3, 64, (1, 4), (1, 4))
        self.convs = (self.conv1, self.conv2, self.conv3, self.conv4)
        self.fc1 = nn.Linear(64 * 15 * 4, 128)
        self.fc2 = nn.Linear(128, 1)

    def forward(self, face, actions):
        """
        :param face: 当前状态  2 * 15 * 4
        :param actions: 所有动作 batch_size * 15 * 4
        :return:
        """
        if face.dim() == 3:
            face = face.unsqueeze(0).repeat((actions.shape[0], 1, 1, 1))
        actions = actions.unsqueeze(1)
        state_action = torch.cat((face, actions), dim=1)

        x = torch.cat([f(state_action) for f in self.convs], -1)
        x = x.view(actions.shape[0], -1)
        x = F.relu(self.fc1(x))
        x = torch.tanh(self.fc2(x))
        return x

    def save(self, name, folder=None):
        if folder is None:
            folder = os.path.join(WORK_DIR, 'models')
        if not os.path.exists(folder):
            os.makedirs(folder)
        path = os.path.join(folder, name)
        torch.save(self.state_dict(), path)

    def load(self, name, folder=None):
        if folder is None:
            folder = os.path.join(WORK_DIR, 'models')
        path = os.path.join(folder, name)
        map_location = 'cpu' if DEVICE.type == 'cpu' else 'gpu'
        static_dict = torch.load(path, map_location)
        self.load_state_dict(static_dict)
        self.eval()


class CQL:
    def __init__(self):
        super(CQL, self).__init__()
        self.time_step = 0
        self.epsilon = conf.EPSILON_HIGH
        self.replay_buffer = deque(maxlen=conf.REPLAY_SIZE)

        self.policy_net = Net().to(DEVICE)
        self.target_net = Net().to(DEVICE)
        self.target_net.load_state_dict(self.policy_net.state_dict())
        self.optimizer = optim.Adam(self.policy_net.parameters(), 1e-4)

    def perceive(self, state, action, reward, next_state, next_action, done):
        self.replay_buffer.append((
            state, action, reward, next_state, next_action, done))
        if len(self.replay_buffer) < conf.BATCH_SIZE:
            return

        # training
        samples = random.sample(self.replay_buffer, conf.BATCH_SIZE)
        s0, a0, r1, s1, a1, done = zip(*samples)
        s0 = torch.stack(s0)
        a0 = torch.stack(a0)
        r1 = torch.tensor(r1, dtype=torch.float).view(conf.BATCH_SIZE, -1).to(DEVICE)
        s1 = torch.stack(s1)
        a1 = torch.stack(a1)
        done = torch.tensor(done, dtype=torch.float).view(conf.BATCH_SIZE, -1).to(DEVICE)

        s1_reward = self.target_net(s1, a1).detach()
        y_true = r1 + (1 - done) * conf.GAMMA * s1_reward
        y_pred = self.policy_net(s0, a0)

        loss = nn.MSELoss()(y_true, y_pred)
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()
        if self.time_step % conf.UPDATE_TARGET_EVERY == 0:
            self.target_net.load_state_dict(self.policy_net.state_dict())

    def e_greedy_action(self, face, actions):
        """
        :param face: 当前状态  2 * 15 * 4
        :param actions: 所有动作 batch_size * 15 * 4
        :return: action: 选择的动作 15 * 4
        """
        q_value = self.policy_net(face, actions).detach()
        if random.random() <= self.epsilon:
            idx = np.random.randint(0, actions.shape[0])
        else:
            idx = torch.argmax(q_value).item()
        self.time_step += 1
        self._update_epsilon()
        return actions[idx]

    def greedy_action(self, face, actions):
        """
        :param face: 当前状态  2 * 15 * 4
        :param actions: 所有动作 batch_size * 15 * 4
        :return: action: 选择的动作 15 * 4
        """
        q_value = self.policy_net(face, actions).detach()
        idx = torch.argmax(q_value).item()
        return actions[idx]

    def _update_epsilon(self):
        self.epsilon = conf.EPSILON_LOW + \
                       (conf.EPSILON_HIGH - conf.EPSILON_LOW) * \
                       np.exp(-1.0 * self.time_step / conf.DECAY)


def lord_ai_play():
    env = Env(debug=False)
    lord = CQL()
    max_win = -1
    total_lord_win, total_farmer_win = 0, 0
    recent_lord_win, recent_farmer_win = 0, 0
    start_time = time.time()
    for episode in range(1, conf.EPISODE + 1):
        print(episode)
        env.reset()
        env.prepare()
        r = 0
        while r == 0:  # r == -1 地主赢， r == 1，农民赢
            # lord first
            state = env.face
            action = lord.e_greedy_action(state, env.valid_actions)
            _, r, _ = env.step_manual(action)
            if r == -1:  # 地主赢
                reward = 1
            else:
                _, r, _ = env.step_auto()  # 下家
                if r == 0:
                    _, r, _ = env.step_auto()  # 上家
                if r == 0:
                    reward = 0
                else:  # r == 1，地主输
                    reward = -1
            done = (r != 0)
            if done:
                next_action = torch.zeros((15, 4), dtype=torch.float).to(DEVICE)
            else:
                next_action = lord.greedy_action(env.face, env.valid_actions)
            lord.perceive(state, action, reward, env.face, next_action, done)

        # print(env.left)
        if r == -1:
            total_lord_win += 1
            recent_lord_win += 1
        else:
            total_farmer_win += 1
            recent_farmer_win += 1

        if episode % 100 == 0:
            end_time = time.time()
            logger.info('Last 100 rounds takes {:.2f}seconds\n'
                        '\tLord recent 100 win rate: {:.2%}\n'
                        '\tLord total {} win rate: {:.2%}\n\n'
                        .format(end_time - start_time,
                                recent_lord_win / 100,
                                episode, total_lord_win / episode))
            if recent_lord_win > max_win:
                max_win = recent_lord_win
                lord.policy_net.save('{}_{}_{}.bin'
                                     .format(BEGIN, episode, max_win))
            recent_lord_win, recent_farmer_win = 0, 0
            start_time = time.time()


if __name__ == '__main__':
    lord_ai_play()