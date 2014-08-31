
import logging

import numpy as np

import nengo
import nengo_mpi

import matplotlib.pyplot as plt

logger = logging.getLogger(__name__)

N = 30
val = 0.5

m = nengo.Network(label='simple', seed=123)
with m:
    input = nengo.Node(output=val, label='input')
    A = nengo.Ensemble(n_neurons=N, dimensions=1, label='A')
    B = nengo.Ensemble(n_neurons=N, dimensions=1, label='B')

    nengo.Connection(input, A)
    nengo.Connection(A, B)
    nengo.Connection(B, A)
    #in_p = nengo.Probe(input, 'output')
    #A_p = nengo.Probe(A, 'decoded_output', synapse=0.1) 
    #spike_probe = nengo.Probe(A, 'spikes')

partition = {input: 0, A: 0, B: 1}

sim = nengo_mpi.Simulator(m, dt=0.001, fixed_nodes=partition)
sim.run(1.0)

#t = sim.trange()
#plt.plot(t, sim.data[in_p], label='Input')
#plt.plot(t, sim.data[A_p], label='Neuron approximation, pstc=0.1')
#plt.ylim((-0.1, val+.1))
#plt.legend(loc=0)
#plt.savefig('test_ensemble.test_constant_scalar.pdf')
#plt.close()
