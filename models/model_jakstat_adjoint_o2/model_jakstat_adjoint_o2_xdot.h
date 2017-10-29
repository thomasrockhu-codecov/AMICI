#ifndef _am_model_jakstat_adjoint_o2_xdot_h
#define _am_model_jakstat_adjoint_o2_xdot_h

#include <sundials/sundials_types.h>
#include <sundials/sundials_nvector.h>
#include <sundials/sundials_sparse.h>
#include <sundials/sundials_direct.h>

using namespace amici;

namespace amici {
class UserData;
class ReturnData;
class TempData;
class ExpData;
}

void xdot_model_jakstat_adjoint_o2(realtype t, N_Vector x, N_Vector dx, N_Vector xdot, void *user_data);


#endif /* _am_model_jakstat_adjoint_o2_xdot_h */
