#ifndef NENGO_MPI_MPI_OPS_HPP
#define NENGO_MPI_MPI_OPS_HPP

#include <mpi.h>
#include <boost/mpi.hpp>
#include <boost/mpi/environment.hpp>
#include <boost/mpi/communicator.hpp>
#include <boost/mpi/intercommunicator.hpp>

#include "operator.hpp"

namespace mpi = boost::mpi;
using namespace std;

class MPIWait;

// Each MPISend and MPIRecv operator has a corresponding MPIWait
// operator, which completes the isend/irecv. The MPIWait operator
// should occur before the MPISend/Receive in the operator ordering
// in MpiSimulatorChunk.

//This all assumes that communication happens on nengo signals that
//are UPDATED (rather than just incremented or set).

//We use mpi tags to identify signals. The tags will be the address of
//the corresponding python signal.

//In INTERLEAVED mode the MPISend operator should be called directly
//after its content vector is updated by an operation. The corresponding
//MPIWait operator should be called directly before the content vector is
//updated.
class MPISend: public Operator{
public:
    MPISend(){};
    MPISend(int dst, int tag, Vector* content);
    void operator()();
    virtual void print(ostream &out) const;
    void set_waiter(MPIWait* mpi_wait);

    int tag;

private:
    int dst;
    mpi::communicator comm;
    Vector* content;
    MPIWait* waiter;
    mpi::request request;

    friend class boost::serialization::access;

    template<class Archive>
    void serialize(Archive & ar, const unsigned int version){
        ar & boost::serialization::base_object<Operator>(*this);
        ar & dst;
        ar & tag;
        ar & content;
        ar & waiter;
    }
};

//In INTERLEAVED mode the MPIRecv operator should be called directly
//after all operators that make use of its content vector have been called.
//The corresponding MPIWait operator should be called directly before any of
//the operators that make use of the content vector.
class MPIRecv: public Operator{
public:
    MPIRecv(){};
    MPIRecv(int src, int tag, Vector* content);
    void operator()();
    virtual void print(ostream &out) const;
    void set_waiter(MPIWait* mpi_wait);

    int tag;

private:
    int src;
    mpi::communicator comm;
    Vector* content;
    MPIWait* waiter;
    mpi::request request;

    friend class boost::serialization::access;

    template<class Archive>
    void serialize(Archive & ar, const unsigned int version){
        ar & boost::serialization::base_object<Operator>(*this);
        ar & src;
        ar & tag;
        ar & content;
        ar & waiter;
    }
};

// Used to complete isend/irecv operations.
class MPIWait: public Operator{

    // So they can access request pointer
    friend class MPISend;
    friend class MPIRecv;

public:
    MPIWait(){};
    MPIWait(int tag);
    void operator()();
    virtual void print(ostream &out) const;

    int tag;

private:
    bool first_call;
    mpi::request* request;

    friend class boost::serialization::access;

    template<class Archive>
    void serialize(Archive & ar, const unsigned int version){
        ar & boost::serialization::base_object<Operator>(*this);
    }
};

#endif
