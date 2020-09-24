"""
Finite Difference Check
-----------------------
This module provides functions to automatically check correctness of amici
computed sensitivities using finite difference approximations
"""

from . import (
    runAmiciSimulation, SensitivityOrder, AMICI_SUCCESS, SensitivityMethod,
    Model, Solver, ExpData, ReturnData, ParameterScaling)
import numpy as np
import copy

from typing import Callable, Optional, List, Sequence


def assert_fun(x):
    assert x


def check_finite_difference(x0: Sequence[float],
                            model: Model,
                            solver: Solver,
                            edata: ExpData,
                            ip: int,
                            fields: List[str],
                            assert_fun: Callable,
                            atol: Optional[float] = 1e-4,
                            rtol: Optional[float] = 1e-4,
                            epsilon: Optional[float] = 1e-3) -> None:
    """
    Checks the computed sensitivity based derivatives against a finite
    difference approximation.

    :param x0:
        parameter value at which to check finite difference approximation

    :param model:
        amici model

    :param solver:
        amici solver

    :param edata:
        exp data

    :param ip:
        parameter index

    :param fields:
        rdata fields for which to check the gradient

    :param assert_fun:
        function that asserts the return values of comparison, enables
        passing of custom assert function from testing frameworks

    :param atol:
        absolute tolerance for comparison

    :param rtol:
        relative tolerance for comparison

    :param epsilon:
        finite difference step-size

    """
    og_sensitivity_order = solver.getSensitivityOrder()
    og_parameters = model.getParameters()
    og_plist = model.getParameterList()

    # sensitivity
    p = copy.deepcopy(x0)
    plist = [ip]

    model.setParameters(p)
    model.setParameterList(plist)

    # simulation with gradient
    if int(og_sensitivity_order) < int(SensitivityOrder.first):
        solver.setSensitivityOrder(SensitivityOrder.first)
    rdata = runAmiciSimulation(model, solver, edata)
    assert_fun(rdata['status'] == AMICI_SUCCESS)

    # finite difference
    solver.setSensitivityOrder(SensitivityOrder.none)

    pf = copy.deepcopy(x0)
    pb = copy.deepcopy(x0)
    pscale = model.getParameterScale()[ip]
    if x0[ip] == 0 or pscale != int(ParameterScaling.none):
        pf[ip] += epsilon / 2
        pb[ip] -= epsilon / 2
    else:
        pf[ip] *= 1 + epsilon / 2
        pb[ip] /= 1 + epsilon / 2

    # forward:
    model.setParameters(pf)
    rdataf = runAmiciSimulation(model, solver, edata)
    assert_fun(rdataf['status'] == AMICI_SUCCESS)

    # backward:
    model.setParameters(pb)
    rdatab = runAmiciSimulation(model, solver, edata)
    assert_fun(rdatab['status'] == AMICI_SUCCESS)

    for field in fields:
        sensi_raw = rdata[f's{field}']
        fd = (rdataf[field]-rdatab[field])/(pf[ip] - pb[ip])
        if len(sensi_raw.shape) == 1:
            sensi = sensi_raw[0]
        elif len(sensi_raw.shape) == 2:
            sensi = sensi_raw[:, 0]
        elif len(sensi_raw.shape) == 3:
            sensi = sensi_raw[:, 0, :]
        else:
            assert_fun(False)  # not implemented
            return

        check_close(sensi, fd, assert_fun, atol, rtol, field, ip=ip)

    solver.setSensitivityOrder(og_sensitivity_order)
    model.setParameters(og_parameters)
    model.setParameterList(og_plist)


def check_derivatives(model: Model,
                      solver: Solver,
                      edata: Optional[ExpData] = None,
                      assert_fun: Optional[Callable] = assert_fun,
                      atol: Optional[float] = 1e-4,
                      rtol: Optional[float] = 1e-4,
                      epsilon: Optional[float] = 1e-3,
                      check_least_squares: bool = True,
                      skip_zero_pars: bool = False) -> None:
    """
    Finite differences check for likelihood gradient.

    :param model:
        amici model

    :param solver:
        amici solver

    :param edata:
        exp data

    :param assert_fun:
        function that asserts the return values of comparison, enables
        passing of custom assert function from testing frameworks

    :param atol:
        absolute tolerance for comparison

    :param rtol:
        relative tolerance for comparison

    :param epsilon:
        finite difference step-size

    :param check_least_squares:
        whether to check least squares related values.

    :param skip_zero_pars:
        whether to perform FD checks for parameters that are zero

    """
    p = np.array(model.getParameters())

    og_sens_order = solver.getSensitivityOrder()

    if int(og_sens_order) < int(SensitivityOrder.first):
        solver.setSensitivityOrder(SensitivityOrder.first)
    rdata = runAmiciSimulation(model, solver, edata)
    solver.setSensitivityOrder(og_sens_order)

    assert_fun(rdata['status'] == AMICI_SUCCESS)

    fields = []
    if edata is not None:
        fields.append('llh')

    if solver.getSensitivityMethod() == SensitivityMethod.forward and \
            solver.getSensitivityOrder() <= SensitivityOrder.first:
        fields.append('x')

    leastsquares_applicable = \
        solver.getSensitivityMethod() == SensitivityMethod.forward \
        and edata is not None

    if 'ssigmay' in rdata.keys() \
            and rdata['ssigmay'] is not None \
            and rdata['ssigmay'].any():
        leastsquares_applicable = False

    if check_least_squares and leastsquares_applicable:
        fields += ['res', 'y']

        check_results(rdata, 'FIM',
                      np.dot(rdata['sres'].transpose(), rdata['sres']),
                      assert_fun,
                      1e-8, 1e-4)
        check_results(rdata, 'sllh',
                      -np.dot(rdata['res'].transpose(), rdata['sres']),
                      assert_fun,
                      1e-8, 1e-4)
    for ip, pval in enumerate(p):
        if pval == 0.0 and skip_zero_pars:
            continue
        check_finite_difference(p, model, solver, edata, ip, fields,
                                assert_fun, atol=atol, rtol=rtol,
                                epsilon=epsilon)


def check_close(result: np.array,
                expected: np.array,
                assert_fun: Callable,
                atol: float,
                rtol: float,
                field: str,
                ip: Optional[int] = None) -> None:
    """
    Compares computed values against expected values and provides rich
    output information.

    :param result:
        computed values

    :param expected:
        expected values

    :param field:
        rdata field for which the gradient is checked, only for error reporting

    :param assert_fun:
        function that asserts the return values of comparison, enables
        passing of custom assert function from testing frameworks

    :param atol:
        absolute tolerance for comparison

    :param rtol:
        relative tolerance for comparison

    :param ip:
        parameter index

    """
    close = np.isclose(result, expected, atol=atol, rtol=rtol, equal_nan=True)

    if not close.all():
        if ip is None:
            index_str = ''
            check_type = 'Regression check  '
        else:
            index_str = f'at index ip={ip} '
            check_type = 'FD check '
        print(f'{check_type} failed for {field} {index_str}for '
              f'{close.size - close.sum()} indices:')
        adev = np.abs(result - expected)
        rdev = np.abs((result - expected)/(expected + atol))
        print(f'max(adev): {adev.max()}, max(rdev): {rdev.max()}')

    assert_fun(close.all())


def check_results(rdata: ReturnData,
                  field: str,
                  expected: np.array,
                  assert_fun: Callable,
                  atol: float,
                  rtol: float) -> None:
    """
    Checks whether rdata[field] agrees with expected according to provided
    tolerances.

    :param rdata:
        simulation results as returned by
        :meth:`amici.amici.runAmiciSimulation`

    :param field:
        name of the field to check

    :param expected:
        expected values

    :param assert_fun:
        function that asserts the return values of comparison, enables
        passing of custom assert function from testing frameworks

    :param atol:
        absolute tolerance for comparison

    :param rtol:
        relative tolerance for comparison
    """

    result = rdata[field]
    if type(result) is float:
        result = np.array(result)

    check_close(result, expected, assert_fun, atol, rtol, field)
