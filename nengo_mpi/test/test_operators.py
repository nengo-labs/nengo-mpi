import logging
import numpy as np
import pytest

import nengo
from nengo.utils.numpy import rmse
import nengo_mpi
import matplotlib.pyplot as plt

logger = logging.getLogger(__name__)

def test_reset():

    D = 40
    reset_val = 4.0

    def init_mpi(sim):
        sim._probe_outputs = {3:[]}

        A = np.random.random((D,D))
        X = 2 * np.ones(D)
        Y = np.zeros(D)

        sim.add_signal(0, A)
        sim.add_signal(1, X)
        sim.add_signal(2, Y)

        sim.add_dot_inc(0, 1, 2)
        sim.mpi_sim.create_Reset(2, reset_val)
        sim.add_probe(2, 2, 2)

    sim = nengo_mpi.Simulator(model=None, init_mpi=init_mpi)
    sim.run(1.0)
    t = sim.trange(dt=.001)
    plt.plot(t, sim.data[2])
    plt.savefig('test_reset.pdf')
    plt.close()

    assert rmse(sim.data[2], reset_val) < 0.001

def test_copy():

    D = 40

    all_data = []
    def make_random():
        data = np.random.random(D)
        all_data.append(data)
        return data

    def init_mpi(sim):

        A = np.random.random((D,D))
        X = 2 * np.ones(D)
        Y = np.zeros(D)
        Z = np.random.random(D)

        sim.add_signal(0, A)
        sim.add_signal(1, X)
        sim.add_signal(2, Y)
        sim.add_signal(3, Z)

        sim.mpi_sim.create_PyFunc(3, make_random, False)
        sim.add_dot_inc(0, 1, 2)
        sim.mpi_sim.create_Copy(2, 3)
        sim.add_probe(2, 2, 2)
        sim.add_probe(3, 3, 3)

    sim = nengo_mpi.Simulator(model=None, init_mpi=init_mpi)
    sim.run(1.0)
    t = sim.trange(dt=.001)
    plt.plot(t, sim.data[2][:, 0])
    plt.plot(t, sim.data[3][:, 0])
    plt.savefig('test_copy.pdf')
    plt.close()

    all_data = np.array(all_data)
    assert rmse(sim.data[2], sim.data[3]) < 0.001
    assert rmse(sim.data[2], all_data) < 0.001

def test_lif():
    """Test that the dynamic model approximately matches the rates."""
    D = 40
    tau_rc = 0.02
    tau_ref = 0.002
    dt = 0.001

    J = np.arange(-2, 2, .1)

    def make_random():
        #data = np.random.random(D)
        #all_data.append(data)
        return J

    def init_mpi(sim):

        A = np.zeros(D)
        B = np.zeros(D)

        sim.add_signal(0, A)
        sim.add_signal(1, B)

        sim.mpi_sim.create_PyFunc(0, make_random, False)
        sim.mpi_sim.create_SimLIF(D, tau_rc, tau_ref, dt, 0, 1)
        sim.add_probe(1, 1, 1)

    sim = nengo_mpi.Simulator(model=None, init_mpi=init_mpi)
    sim.run(1.0)

    t = sim.trange(dt=.001)
    spikes = sim.data[1]
    plt.plot(t, spikes[:, 0:10])
    plt.savefig('test_lif.pdf')
    plt.close()

    #rng = np.random.RandomState(85243)

    #spikes = np.zeros((t_final / dt,) + J.shape)
    J = J.reshape(-1, 1)

    math_rates = nengo.LIF(D, tau_rc=tau_rc, tau_ref=tau_ref).rates(J, gain=np.ones(D), bias=np.zeros(D))
    sim_rates = spikes.sum(0)

    plt.plot(J, sim_rates, label='sim')
    plt.plot(J, math_rates, label='math')
    plt.savefig('test_lif.pdf')
    plt.close()
    assert np.allclose(sim_rates, math_rates, atol=1, rtol=0.02)


#def test_lif_base(nl_nodirect):
#    """Test that the dynamic model approximately matches the rates"""
#    rng = np.random.RandomState(85243)
#
#    dt = 0.001
#    n = 5000
#    x = 0.5
#    max_rates = rng.uniform(low=10, high=200, size=n)
#    intercepts = rng.uniform(low=-1, high=1, size=n)
#
#    m = nengo.Network()
#    with m:
#        ins = nengo.Node(x)
#        ens = nengo.Ensemble(
#            nl_nodirect(n), 1, max_rates=max_rates, intercepts=intercepts)
#        nengo.Connection(ins, ens.neurons, transform=np.ones((n, 1)))
#        spike_probe = nengo.Probe(ens.neurons, "output")
#
#    sim = nengo.Simulator(m, dt=dt)
#
#    t_final = 1.0
#    sim.run(t_final)
#    spikes = sim.data[spike_probe].sum(0)
#
#    math_rates = ens.neurons.rates(
#        x, *ens.neurons.gain_bias(max_rates, intercepts))
#    sim_rates = spikes / t_final
#    logger.debug("ME = %f", (sim_rates - math_rates).mean())
#    logger.debug("RMSE = %f",
#                 rms(sim_rates - math_rates) / (rms(math_rates) + 1e-20))
#    assert np.sum(math_rates > 0) > 0.5 * n, (
#        "At least 50% of neurons must fire")
#    assert np.allclose(sim_rates, math_rates, atol=1, rtol=0.02)


if __name__ == "__main__":
    nengo.log(debug=True)
    pytest.main([__file__, '-v'])