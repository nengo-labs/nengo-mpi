#ifndef NENGO_MPI_MPI_SIMULATOR_HPP
#define NENGO_MPI_MPI_SIMULATOR_HPP

#include <list>
#include <vector>
#include <string>
#include <memory>
#include <exception>

#include <mpi.h>

#include "simulator.hpp"
#include "spec.hpp"
#include "chunk.hpp"
#include "psim_log.hpp"

using namespace std;

const int setup_tag = 1;
const int probe_tag = 2;

class MpiSimulator: public Simulator{
public:
    MpiSimulator(bool mpi_merged, bool collect_timings);

    // Used when we need to spawn extra process (e.g. when run through python)
    MpiSimulator(int n_processors, dtype dt, bool mpi_merged, bool collect_timings);

    ~MpiSimulator();

    void spawn_processors();
    void init();

    void from_file(string filename) override;
    void finalize_build() override;

    void run_n_steps(int steps, bool progress, string log_filename) override;

    void gather_probe_data() override;

    void close() override;

    string to_string() const;

    friend ostream& operator << (ostream &out, const MpiSimulator &sim){
        out << sim.to_string();
        return out;
    }

protected:
    int n_processors;
    bool mpi_merged;

    MPI_Comm comm;

    // Map from a source index to number of probes. Used to gather
    // probe data from remote chunks after simulation.
    map<int, int> probe_counts;
};

string recv_string(int src, int tag, MPI_Comm comm);
void send_string(string s, int dst, int tag, MPI_Comm comm);

dtype recv_dtype(int src, int tag, MPI_Comm comm);
void send_dtype(dtype d, int dst, int tag, MPI_Comm comm);

int recv_int(int src, int tag, MPI_Comm comm);
void send_int(int i, int dst, int tag, MPI_Comm comm);

key_type recv_key(int src, int tag, MPI_Comm comm);
void send_key(key_type i, int dst, int tag, MPI_Comm comm);

unique_ptr<BaseSignal> recv_matrix(int src, int tag, MPI_Comm comm);
void send_matrix(unique_ptr<BaseSignal> matrix, int dst, int tag, MPI_Comm comm);

int bcast_recv_int(MPI_Comm comm);
void bcast_send_int(int i, MPI_Comm comm);

#endif
