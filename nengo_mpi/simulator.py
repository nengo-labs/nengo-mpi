from nengo.simulator import ProbeDict
import nengo.utils.numpy as npext
from nengo.cache import get_default_decoder_cache

from model import MpiBuilder, MpiModel
from partition import Partitioner

import numpy as np

import logging
logger = logging.getLogger(__name__)


class Simulator(object):
    """MPI simulator for nengo 2.0."""

    def __init__(
            self, network, dt=0.001, seed=None, model=None,
            partitioner=None, save_file=""):
        """
        Most of the time, you will pass in a network and sometimes a dt::

            sim1 = nengo.Simulator(my_network)  # Uses default 0.001s dt
            sim2 = nengo.Simulator(my_network, dt=0.01)  # Uses 0.01s dt

        Parameters
        ----------
        network : nengo.Network
            A network object to be built and then simulated.

        dt : float
            The length of a simulator timestep, in seconds.

        seed : int
            A seed for all stochastic operators used in this simulator.
            Note that there are not stochastic operators implemented
            currently, so this parameters does nothing.

        model : nengo.builder.Model
            A model object that contains build artifacts to be simulated.
            Usually the simulator will build this model for you; however,
            if you want to build the network manually, or to inject some
            build artifacts in the Model before building the network,
            then you can pass in an instance of ``MpiModel'' instance
            or a ``nengo.builder.Model`` instance. If the latter, it
            will be converted into an ``MpiModel''.

        partitioner: Partitioner
            Specifies how to assign nengo objects to MPI processes.

        save_file: string
            Name of file that will store all data added to the simulator.
            The simulator can later be reconstructed from this file. If
            equal to the empty string, then no file is created.
        """

        # Note: seed is not used right now, but one day...
        assert seed is None, "Simulator seed not yet implemented"
        self.seed = np.random.randint(npext.maxint) if seed is None else seed

        self.n_steps = 0
        self.dt = dt

        if partitioner is None:
            partitioner = Partitioner()

        print "Partitioning network..."
        num_components, assignments = partitioner.partition(network)

        print "Building MPI model..."
        self.model = MpiModel(
            num_components, assignments, dt=dt,
            label="%s, dt=%f" % (network, dt),
            decoder_cache=get_default_decoder_cache(),
            save_file=save_file)

        MpiBuilder.build(self.model, network)

        print "Finalizing MPI model..."
        self.model.finalize_build()

        # probe -> python list
        self._probe_outputs = self.model.params

        self.data = ProbeDict(self._probe_outputs)

        print "MPI model ready."

    @property
    def mpi_sim(self):
        if not self.model.runnable:
            raise Exception(
                "Cannot access C++ simulator of MpiModel, MpiModel is "
                "not in a runnable state. Likely in write-file mode.")

        return self.model.mpi_sim

    def __str__(self):
        return self.mpi_sim.to_string()

    def run_steps(self, steps, progress_bar, log_filename):
        """Simulate for the given number of `dt` steps."""

        print "Simulating MPI model for %d steps..." % steps
        self.mpi_sim.run_n_steps(steps, progress_bar, log_filename)

        if not log_filename:
            for probe, probe_key in self.model.probe_keys.items():
                data = self.mpi_sim.get_probe_data(probe_key, np.empty)

                if probe not in self._probe_outputs:
                    self._probe_outputs[probe] = data
                else:
                    self._probe_outputs[probe].extend(data)

        self.n_steps += steps

        print "MPI Simulation complete."

    def step(self):
        """Advance the simulator by `self.dt` seconds."""
        self.run_steps(1)

    def run(self, time_in_seconds, progress_bar=True, log_filename=""):
        """Simulate for the given length of time."""

        steps = int(np.round(float(time_in_seconds) / self.dt))
        self.run_steps(steps, progress_bar, log_filename)

    def trange(self, dt=None):
        dt = self.dt if dt is None else dt
        n_steps = int(self.n_steps * (self.dt / dt))
        return dt * np.arange(1, n_steps + 1)

    def reset(self):
        self.n_steps = 0
        self.mpi_sim.reset()
