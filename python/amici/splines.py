"""
Splines
------------
This module provides helper functions for reading/writing splines
with AMICI annotations from/to SBML files
and for adding such splines to the AMICI C++ code.
"""

from __future__ import annotations
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from .sbml_import import SbmlImporter
    from typing import (
        Sequence,
        Union,
        Tuple,
        Optional,
        Dict,
        Any,
        List,
    )

import collections

import xml.etree.ElementTree as ET
import numpy as np
import sympy as sp
import libsbml

from abc import abstractmethod, ABC
from itertools import count
from sympy.core.parameters import evaluate
from sympy import cxxcode

from .sbml_utils import (
    sbml_time_symbol,
    amici_time_symbol,
    pretty_xml,
    mathml2sympy,
    sbmlMathML,
    annotation_namespace,
    getSbmlUnits,
    hasParameter,
    addParameter,
    addAssignmentRule,
)


def sympify_noeval(x):
    with evaluate(False):
        return sp.sympify(x)


################################################################################


class UniformGrid(collections.abc.Sequence):
    """
    A grid of uniformly-spaced real points, computed with rational arithmetic.
    Implements the `collections.abc.Sequence` interface
    and can be converted to a `numpy.ndarray`
    (conversion to float can be specified with `dtype=float`).
    """

    def __init__(self, start, stop, step):
        """
        Create a new `UniformGrid`.
        The `stop` attribute of the resulting object will be larger
        than the given `stop` argument in the case `stop - start` is not
        an integer multiple of `step`.
        """
        start = sp.nsimplify(sp.sympify(start))
        stop = sp.nsimplify(sp.sympify(stop))
        step = sp.nsimplify(sp.sympify(step))

        if start > stop:
            raise ValueError(
                f'start point {start} greater than stop point {stop}'
            )

        if step <= 0:
            raise ValueError(f'step size {step} must be strictly positive')

        xx = []
        for i in count():
            x = start + i * step
            xx.append(x)
            if x >= stop:
                break

        self._xx = np.asarray(xx)

    @property
    def start(self):
        "First point."
        return self._xx[0]

    @property
    def stop(self):
        "Last point."
        return self._xx[-1]

    @property
    def step(self):
        "Distance between consecutive points."
        if len(self._xx) > 1:
            return self._xx[1] - self._xx[0]
        else:
            return sp.sympify(0)

    def __getitem__(self, i):
        return self._xx[i]

    def __len__(self):
        return len(self._xx)

    def __array__(self, dtype=None):
        if dtype is None:
            return self._xx
        else:
            return np.array(self._xx, dtype=dtype)

    def __repr__(self):
        return f'UniformGrid(start={self.start}, stop={self.stop}, step={self.step})'


################################################################################


class AbstractSpline(ABC):
    """
    Base class for spline functions which can be computed efficiently
    thanks to tailored C++ implementations in AMICI.
    Inside an SBML file, such splines are implemented with
    an assignment rule containing both a symbolic piecewise formula
    for the spline (allowing compatibility with any SBML-aware software)
    and annotations which encode the necessary information for AMICI to
    recreate the spline object (allowing for fast computations when the SBML
    file is used together with AMICI).
    """

    def __init__(
            self,
            sbmlId: Union[str, sp.Symbol],
            x: Union[str, sp.Basic],
            xx: Sequence,
            yy: Sequence,
            *,
            bc: Optional[str] = None,
            extrapolate: Union[
                None, str,
                Tuple[Union[None, str], Union[None, str]]
            ] = None,
            logarithmic_paraterization: bool = False
        ):
        """
        Base constructor for `AbstractSpline` objects.

            :param sbmlId:
            The SBML ID of the parameter associated to the spline as a string or
            a SymPy symbol.

            :param x:
            The symbolic argument at which the spline is evaluated.
            It will be sympified.

            :param xx:
            The points at which the spline values are known.
            Currently they can only contain depend on constant parameters.
            It should be strictly increasing.
            It will be sympified.

            :param yy:
            The spline values at each of the points in `xx`.
            They may not depend on model species.
            It will be sympified.

            :param bc:
            Applied boundary conditions.
            `None` if the boundary condition are not needed for the spline object.

            :param extrapolate:
            Whether to extrapolate the spline outside the base interval
            defined by `(xx[0], xx[-1])`.
            It is a tuple of extrapolation methods, one for each side of the
            base interval.
            `None` or `'no_extrapolation'`
            means no extrapolation is to be performed
            (evaluating the spline outside the base interval is not guaranteed
            to work).
            If it is not a tuple, then the same extrapolation will be applied
            on both sides.

            :param logarithmic_paraterization:
            Interpolation is done in log scale.
        """

        if isinstance(sbmlId, str):
            sbmlId = sp.Symbol(sbmlId)
        elif not isinstance(sbmlId, sp.Symbol):
            raise TypeError(
                'sbmlId must be either a string or a SymPy symbol, '
                f'got {sbmlId} instead'
            )

        x = sympify_noeval(x)
        if not isinstance(x, sp.Basic):
            # It may still be e.g. a list!
            raise ValueError(f'Invalid x = {x}')

        if not isinstance(xx, UniformGrid):
            xx = np.asarray([sympify_noeval(x) for x in xx])
        yy = np.asarray([sympify_noeval(y) for y in yy])

        if len(xx) != len(yy):
            raise ValueError(
                'length of xx and yy must be the same '
                f'(instead len(xx) = {len(xx)} and len(yy) = {len(yy)})'
            )

        if bc == 'periodic' and yy[0] != yy[-1]:
            raise ValueError(
                'if the spline is to be periodic, '
                'the first and last elements of yy must be equal'
            )

        if all(x.is_Number for x in xx) and not np.all(np.diff(xx) >= 0):
            raise ValueError('xx should be stricly increasing')

        if logarithmic_paraterization:
            if all(y.is_Number for y in yy) and any(y <= 0 for y in yy):
                raise ValueError(
                    'when interpolation is done in log-scale, '
                    'yy should be stricly positive'
                )

        extrapolate = AbstractSpline._normalize_extrapolate(extrapolate, bc)

        self._sbmlId = sbmlId
        self._x = x
        self._xx = xx
        self._yy = yy
        self._bc = bc
        self._extrapolate = extrapolate
        self._logarithmic_paraterization = logarithmic_paraterization

        self._formula_cache = {}

    @staticmethod
    def _normalize_extrapolate(extrapolate, bc):
        """
        Preprocess `extrapolate` to a standard form
        and perform consistency checks
        """
        if not isinstance(extrapolate, tuple):
            extrapolate = (extrapolate, extrapolate)
        if len(extrapolate) != 2:
            raise TypeError(
                f'extrapolate should be a 2-tuple, gor {extrapolate} instead'
            )
        valid_extrapolate = (
            'constant',
            'no_extrapolation',
            'periodic',
            None
        )
        for i in (0, 1):
            if extrapolate[i] not in valid_extrapolate:
                raise ValueError(
                    'currently the supported extrapolation methods are: ' +
                    str(valid_extrapolate) +
                    f' (got {extrapolate[i]} instead)'
                )
            if extrapolate[i] == 'no_extrapolation':
                extrapolate[i] = None
            if extrapolate[i] == 'periodic' and bc != 'periodic':
                raise ValueError(
                    'The spline must be satisfy periodic boundary conditions '
                    'in order for periodic extrapolation to be used.'
                )

        if (extrapolate[0] == 'periodic' or extrapolate[1] == 'periodic') and \
                extrapolate[0] != extrapolate[1] and \
                not (extrapolate[0] is None or extrapolate[1] is None):
            raise NotImplementedError(
                'At the moment if periodic extrapolation is applied '
                'to one side, the extrapolation at the other side '
                'must either be periodic or not be applied '
                '(in which case it will be periodic anyway).'
            )

        return extrapolate

    @property
    def sbmlImporter(self) -> sp.Symbol:
        return self._sbmlImporter

    @property
    def sbmlId(self) -> sp.Symbol:
        "SBML ID of the spline parameter."
        return self._sbmlId

    @property
    def x(self) -> sp.Basic:
        "The symbolic argument at which the spline is evaluated."
        return self._x

    @property
    def xx(self) -> np.ndarray:
        "The points at which the spline values are known."
        return self._xx

    @property
    def yy(self) -> np.ndarray:
        "The spline values at each of the points in `xx`."
        return self._yy

    @property
    def bc(self) -> Union[str, None]:
        """
        Return the boundary conditions applied to this spline.
        A value of `None` means that the spline type does not require
        to explicitly define boundary conditions.
        """
        return None

    @property
    def extrapolate(self) -> Tuple[Union[None, str], Union[None, str]]:
        "Whether to extrapolate the spline outside the base interval."
        return self._extrapolate

    @property
    def logarithmic_paraterization(self) -> bool:
        "Whether interpolation is done in log-scale."
        return self._logarithmic_paraterization

    @property
    @abstractmethod
    def method(self) -> str:
        "Spline method."
        return NotImplemented

    def check_if_valid(self, importer: SbmlImporter):
        """
        Check if the spline described by this object can be correctly
        be implemented by AMICI. E.g., check whether the formulas
        for spline grid points, values, ... contain species symbols.
        """
        # TODO this is very much a draft

        fixed_parameters = importer.symbols['fixed_parameter']['identifier']
        species = list(importer.symbols['species']['identifier'])

        for x in self.xx:
            if not x.free_symbols.issubset(fixed_parameters):
                raise ValueError('xx should only depend on constant parameters')

        for y in self.yy:
            if len(y.free_symbols.intersection(species)) != 0:
                raise ValueError('yy should not depend on model species')

        fixed_parameters_values = importer.symbols['fixed_parameter']['value']
        subs = dict(zip(fixed_parameters, fixed_parameters_values))
        xx_values = [sp.simplify(x.subs(subs)) for x in self.xx]
        for x in xx_values:
            assert x.is_Number
        if not np.all(np.diff(xx) >= 0):
            raise ValueError('xx should be stricly increasing')

    def poly(self, i: int, *, x=None) -> sp.Basic:
        """
        Get the polynomial interpolant on the `(xx[i], xx[i+1])` interval
        in Horner form.
        """
        if i < 0:
            i += len(self.xx) - 1

        assert 0 <= i < len(self.xx) - 1

        if x is None:
            x = self.x

        poly = sp.poly(self._poly(x, i), wrt=x)
        return sp.horner(poly)

    @abstractmethod
    def _poly(self, x, i) -> sp.Basic:
        """
        Return the symbolic expression for polynomial interpolant
        on the `(xx[i], xx[i+1])` interval using `x` as the spline parameter.
        """
        return NotImplemented

    def segment_formula(self, i: int, *, x=None) -> sp.Basic:
        if x is None:
            x = self.x
        poly = self.poly(i, x=x)
        if self.logarithmic_paraterization:
            return sp.exp(poly)
        else:
            return poly

    def y_scaled(self, i: int):
        if self.logarithmic_paraterization:
            return sp.log(self.yy[i])
        else:
            return self.yy[i]

    @property
    def extrapolation_formulas(self) \
            -> Tuple[Union[None, sp.Basic], Union[None, sp.Basic]]:
        """
        Returns the extrapolation formulas on the left and right side
        of the interval `(xx[0], xx[-1])`.
        A value of `None` means that no extrapolation is required.
        """
        return self._extrapolation_formulas(self.x)

    def _extrapolation_formulas(self, x):
        extrLeft, extrRight = self.extrapolate

        if extrLeft == 'constant':
            extrLeft = self.yy[0]
        elif extrLeft is not None:
            raise ValueError(f'unsupported extrapolation method {extrLeft}')

        if extrRight == 'constant':
            extrRight = self.yy[-1]
        elif extrRight is not None:
            raise ValueError(f'unsupported extrapolation method {extrRight}')

        return (extrLeft, extrRight)

    @property
    def formula(self) -> sp.Piecewise:
        """
        Compute a symbolic piecewise formula for the spline.
        """
        return self._formula(sbml=False)

    @property
    def sbmlFormula(self) -> sp.Piecewise:
        """
        Compute a symbolic piecewise formula for the spline for use inside
        a SBML assignment rule (e.g., the AMICI time symbol will be replaced
        with its SBML counterpart).
        """
        return self._formula(sbml=True)

    def _formula(self, *, x=None, sbml=False, **kwargs) -> sp.Piecewise:
        if 'extrapolate' in kwargs.keys():
            key = (x, sbml, kwargs['extrapolate'])
        else:
            key = (x, sbml)
        if key in self._formula_cache.keys():
            return self._formula_cache[key]

        if x is None:
            x = self.x
        if 'extrapolate' in kwargs.keys():
            extrapolate = AbstractSpline._normalize_extrapolate(
                kwargs['extrapolate'], self.bc
            )
        else:
            extrapolate = self.extrapolate

        pieces = []

        if extrapolate[0] == 'periodic' or extrapolate[1] == 'periodic':
            if sbml:
                # NB mod is not supported in SBML
                x = sympy.Symbol(self.sbmlId + '_x_in_fundamental_period')
                # NB we will do the parameter substitution in SBML
                #    because the formula for x will be a piecewise
                #    and sympy handles Piecewises inside other Piecewises
                #    really badly.
            else:
                x = self._to_base_interval(x)
            extrLeft, extrRight = None, None
        else:
            extrLeft, extrRight = self._extrapolation_formulas(x)

        if extrLeft is not None:
            pieces.append((extrLeft, x < self.xx[0]))

        for i in range(len(self.xx) - 2):
            pieces.append((self.segment_formula(i, x=x), x < self.xx[i+1]))

        if extrRight is not None:
            pieces.append((self.segment_formula(-1, x=x), x < self.xx[-1]))
            pieces.append((extrRight, sp.sympify(True)))
        else:
            pieces.append((self.segment_formula(-1, x=x), sp.sympify(True)))

        with evaluate(False):
            if sbml:
                pieces = [
                    (
                        p.subs(amici_time_symbol, sbml_time_symbol),
                        c.subs(amici_time_symbol, sbml_time_symbol)
                    )
                    for (p, c) in pieces
                ]
            formula = sp.Piecewise(*pieces)

        self._formula_cache[key] = formula
        return formula

    @property
    def period(self):
        return self.xx[-1] - self.xx[0] if bc == 'periodic' else None

    def _to_base_interval(self, x):
        if bc != 'periodic':
            raise ValueError(
                '_to_base_interval makes no sense with non-periodic bc'
            )
        return self.xx[0] + sp.Mod(x - self.xx[0], self.period)

    def integrate(self, x0, x1):
        x = sp.Dummy('x')
        x0, x1 = sp.sympify((x0, x1))

        if self.extrapolate != ('periodic', 'periodic'):
            return self._formula(x=x).integrate((x, x0, x1))

        else:
            formula = self._formula(x=x, extrapolate=None)

            x00 = _to_base_interval(x0)
            x11 = _to_base_interval(x1)

            assert self.xx[0] <= x00 <= self.xx[-1]
            assert self.xx[0] <= x11 <= self.xx[-1]

            x01 = self.xx[-1] + x0 - x00
            x10 = self.xx[0] + x1 - x11

            if x1 <= x01:
                assert x10 < x01
                return formula.integrate((x, x00, x11))

            else:
                assert x01 <= x10

                if x01 == x10:
                    return formula.integrate((x, x00, self.xx[-1])) + \
                           formula.integrate((x, self.xx[0], x11))

                else:
                    n = (x10 - x01) / self.period
                    assert n.is_Integer
                    assert n > 0
                    return formula.integrate((x, x00, self.xx[-1])) + \
                           n*formula.integrate((x, self.xx[0], self.xx[-1])) + \
                           formula.integrate((x, self.xx[0], x11))

    @property
    def amiciAnnotation(self) -> str:
        """
        An SBML annotation describing the spline.
        """
        annotation = f'<amici:spline xmlns:amici="{annotation_namespace}"'
        for (attr, value) in self._annotation_attributes().items():
            if isinstance(value, bool):
                value = str(value).lower()
            value = f'"{value}"'
            annotation += f' amici:{attr}={value}'
        annotation += '>'

        for (child, grandchildren) in self._annotation_children().items():
            if isinstance(grandchildren, str):
                grandchildren = [grandchildren]
            annotation += f'<amici:{child}>'
            for gc in grandchildren:
                annotation += gc
            annotation += f'</amici:{child}>'

        annotation += '</amici:spline>'

        # Check XML and prettify
        return pretty_xml(annotation)

    def _annotation_attributes(self) -> Dict[str, Any]:
        attributes = {}

        attributes['spline_method'] = self.method

        if self.bc is not None:
            attributes['spline_bc'] = self.bc

        if self.extrapolate[0] == self.extrapolate[1]:
            extr = None if self.extrapolate is None else self.extrapolate[0]
        else:
            extr1, extr2 = self.extrapolate
            extr1 = 'no_extrapolation' if extr1 is None else extr1
            extr2 = 'no_extrapolation' if extr2 is None else extr2
            extr = f'({extr1}, {extr2})'
        if extr is not None:
            attributes['spline_extrapolate'] = extr

        if self.logarithmic_paraterization:
            attributes['spline_logarithmic_paraterization'] = True

        return attributes

    def _annotation_children(self) -> Dict[str, Union[str, List[str]]]:
        children = {}

        with evaluate(False):
            x = self.x.subs(amici_time_symbol, sbml_time_symbol)
        children['spline_parameter'] = sbmlMathML(x)

        if isinstance(self.xx, UniformGrid):
            children['spline_uniform_grid'] = [
                sbmlMathML(self.xx.start),
                sbmlMathML(self.xx.stop),
                sbmlMathML(self.xx.step),
            ]
        else:
            for x in self.xx:
                assert amici_time_symbol not in x.free_symbols
            children['spline_grid'] = [sbmlMathML(x) for x in self.xx]

        children['spline_values'] = [sbmlMathML(y) for y in self.yy]

        return children

    def addToSbmlModel(
            self,
            model: libsbml.Model,
            *,
            auto_add: bool = False,
            x_nominal: Sequence[float] = None,
            y_nominal: Optional[Union[Sequence[float], float]] = None,
            y_units: Optional[str] = None,
            x_units: Optional[str] = None
        ):
        """
        Function add the spline to an SBML model using an assignment rule
        with AMICI-specific annotations.

            :param sbmlImporter:
            A `SbmlImporter` object
            (used for testing whether formulas are adimissible).

            :param model:
            SBML model to which the spline is to be added.

            :param auto_add:
            Automatically missing parameters to the SBML model.
            Only used for expressions consisting in a single symbol.

            :param x_nominal:
            Values used when auto-adding parameters from `xx`.

            :param y_nominal:
            Values used when auto-adding parameters from `yy`.

            :param x_units:
            Units used when auto-adding parameters from `xx`.

            :param y_units:
            Units used when auto-adding parameters from `yy`.
        """
        # Convert time from AMICI to SBML naming
        with evaluate(False):
            x = self.x.subs(amici_time_symbol, sbml_time_symbol)

        # Try to autodetermine units
        if x_units is None:
            x_units = getSbmlUnits(model, x)
            for _x in self.xx:
                if x_units is not None:
                    break
                x_units = getSbmlUnits(model, _x)
        if y_units is None:
            for _y in self.yy:
                y_units = getSbmlUnits(model, _y)
                if y_units is not None:
                    break

        # Autoadd parameters
        if auto_add:
            if not hasParameter(model, self.sbmlId):
                addParameter(model, self.sbmlId, constant=False, units=y_units)

            if isinstance(x_nominal, collections.abc.Sequence):
                if len(x_nominal) != len(self.xx):
                    raise ValueError(
                        'if x_nominal is a list, then it must have '
                        'the same length as the spline grid'
                    )
            else:
                x_nominal = len(self.xx) * [x_nominal]
            for (_x, _val) in zip(self.xx, x_nominal):
                if _x.is_Symbol and not hasParameter(model, _x.name):
                    addParameter(model, _x.name, value=_val, units=x_units)

            if isinstance(y_nominal, collections.abc.Sequence):
                if len(y_nominal) != len(self.yy):
                    raise ValueError(
                        'if y_nominal is a list, then it must have '
                        'the same length as the spline values'
                    )
            else:
                y_nominal = len(self.yy) * [y_nominal]
            for (_y, _val) in zip(self.yy, y_nominal):
                if _y.is_Symbol and not hasParameter(model, _y.name):
                    addParameter(model, _y.name, value=_val, units=y_units)

        # Create assignment rule for spline
        rule = addAssignmentRule(model, self.sbmlId, self.sbmlFormula)

        # Add annotation specifying spline method
        rule.setAnnotation(self.amiciAnnotation)

        # Create additional assignment rule for periodic extrapolation
        # NB mod is not in the subset of MathML supported by SBML
        if self.extrapolate[0] == 'periodic' or self.extrapolate[1] == 'periodic':
            parameterId = self.sbmlId + '_x_in_fundamental_period'
            T = self.xx[-1] - self.xx[0]
            x0 = self.xx[0]
            s = 2 * sp.pi * ((x - x0) / T - sp.sympify(1)/4)
            k = sp.Piecewise((3, sp.cos(s) < 0), (1, True))
            formula = x0 + T * (sp.atan(sp.tan(s)) / (2 * sp.pi) + k/4)
            assert amici_time_symbol not in formula.free_symbols
            par = addParameter(model, parameterId, constant=False, units=x_units)
            par.setAnnotation(
                f'<amici:discard xmlns:amici="{annotation_namespace}">'
            )
            addAssignmentRule(model, parameterId, formula)

    # def _replace_sbml_time_with_amici_time(self):
    #     self._replace_in_all_expressions(
    #         sbml_time_symbol, amici_time_symbol
    #     )

    def _replace_in_all_expressions(self, old: sp.Symbol, new: sp.Symbol):
        self._x = self.x.subs(old, new)
        if not isinstance(self.xx, UniformGrid):
            self._xx = [x.subs(old, new) for x in self.xx]
        self._yy = [y.subs(old, new) for y in self.yy]

    @staticmethod
    def isSpline(rule: libsbml.AssignmentRule):
        """
        Determine if a SBML AssignmentRule is an AMICI-annotated spline formula.
        """
        return AbstractSpline.getAnnotation(rule) is not None

    @staticmethod
    def getAnnotation(rule: libsbml.AssignmentRule):
        if not isinstance(rule, libsbml.AssignmentRule):
            raise TypeError('rule must be an AssignmentRule')
        if rule.isSetAnnotation():
            annotation = ET.fromstring(rule.getAnnotationString())
            for child in annotation.getchildren():
                if child.tag == f'{{{annotation_namespace}}}spline':
                    return child
        return None

    @staticmethod
    def fromAnnotation(sbmlId: sp.Symbol, annotation: ET.Element, *, locals):
        """
        Create a spline object from a SBML annotation.
        """

        if annotation.tag != f'{{{annotation_namespace}}}spline':
            raise ValueError(
                'The given annotation is not an AMICI SBML annotation.'
            )

        attributes = {}
        for key, value in annotation.items():
            if not key.startswith(f'{{{annotation_namespace}}}'):
                raise ValueError(
                    f'unexpected attribute {key} inside spline annotation'
                )
            key = key[len(annotation_namespace)+2:]
            if value == 'true':
                value = True
            elif value == 'false':
                value = False
            attributes[key] = value

        children = {}
        for child in annotation.getchildren():
            if not child.tag.startswith(f'{{{annotation_namespace}}}'):
                raise ValueError(
                    f'unexpected node {child.tag} inside spline annotation'
                )
            key = child.tag[len(annotation_namespace)+2:]
            value = [
                mathml2sympy(
                    ET.tostring(gc).decode(),
                    evaluate=False,
                    locals=locals,
                    expression_type='Rule'
                )
                for gc in child.getchildren()
            ]
            children[key] = value

        if attributes['spline_method'] == 'cubic_hermite':
            cls = CubicHermiteSpline
        else:
            raise ValueError(
                f"unknown spline method {attributes['spline_method']}"
            )

        del attributes['spline_method']
        kwargs = cls._fromAnnotation(attributes, children)

        if len(attributes) != 0:
            raise ValueError(
                'Unprocessed attributes in spline annotation:\n' +
                str(attributes)
            )

        if len(children) != 0:
            raise ValueError(
                'Unprocessed children in spline annotation:\n' +
                str(children)
            )

        return cls(sbmlId, **kwargs)

    @classmethod
    def _fromAnnotation(cls, attributes, children):
        kwargs = {}

        kwargs['bc'] = attributes.pop('spline_bc', None)

        extr = attributes.pop('spline_extrapolate', None)
        if isinstance(extr, str) and extr.startswith('('):
            if not extr.endswith(')'):
                raise ValueError(f'Ill-formatted extrapolation {extr}')
            extr_cmps = extr[1:-1].split(',')
            if len(extr_cmps) != 2:
                raise ValueError(f'Ill-formatted extrapolation {extr}')
            extr = (extr_cmps[0].strip(), extr_cmps[1].strip())
        kwargs['extrapolate'] = extr

        kwargs['logarithmic_paraterization'] = \
            attributes.pop('spline_logarithmic_paraterization', False)

        if 'spline_parameter' not in children.keys():
            raise ValueError(
                "required spline annotation 'spline_parameter' missing"
            )
        x = children.pop('spline_parameter')
        if len(x) != 1:
            raise ValueError(
                "Ill-formatted spline annotation 'spline_parameter' " +
                "(more than one children is present)"
            )
        kwargs['x'] = x[0]

        if 'spline_uniform_grid' in children.keys():
            start, stop, step = children.pop('spline_uniform_grid')
            kwargs['xx'] = UniformGrid(start, stop, step)
        elif 'spline_grid' in children.keys():
            kwargs['xx'] = children.pop('spline_grid')
        else:
            raise ValueError(
                "spline annotation requires either "
                "'spline_grid' or 'spline_uniform_grid' to be specified."
            )

        if 'spline_values' not in children.keys():
            raise ValueError(
                "required spline annotation 'spline_values' missing"
            )
        kwargs['yy'] = children.pop('spline_values')

        return kwargs

    def parameters(self, importer: SbmlImporter):
        return self._parameters().intersection(
            set(importer.symbols['parameter']['identifier'])
        )

    def _parameters(self):
        parameters = set()
        for y in self.yy:
            parameters.update(y.free_symbols)
        return parameters

    def odeModelSymbol(self, importer: SbmlImporter):
        parameters = list(self.parameters(importer))

        class AmiciSpline(sp.Function):
            # AmiciSpline(splineId, x, *parameters)
            nargs = (len(parameters) + 2, )

            @classmethod
            def eval(cls, *args):
                return None  # means leave unevaluated

            def fdiff(self, argindex=1):
                if argindex == 1:
                    # Derivative with respect to the spline SBML ID
                    # Since the SBML ID is not a real function parameter
                    # (more like a subscript), the derivative will be zero
                    return sp.Integer(0)

                elif argindex == 2:
                    class AmiciSplineDerivative(sp.Function):
                        # Spline derivative
                        # AmiciSplineDerivative(splineId, x, *parameters)
                        nargs = (len(parameters) + 2, )

                        @classmethod
                        def eval(cls, *args):
                            return None  # means leave unevaluated

                        def fdiff(self, argindex=1):
                            return NotImplementedError(
                                'Second order derivatives for spline '
                                'are not implemented yet.'
                            )

                        def _eval_is_real(self):
                            return True

                    return AmiciSplineDerivative(*self.args)

                else:
                    pindex = argindex - 3
                    assert 0 <= pindex < len(parameters)

                    class AmiciSplineSensitivity(sp.Function):
                        # Derivative with respect to a parameter paramId
                        # AmiciSplineSensitivity(splineId, x, paramId, *parameters)
                        nargs = (len(parameters) + 3, )

                        @classmethod
                        def eval(cls, *args):
                            return None  # means leave unevaluated

                        def fdiff(self, argindex=1):
                            return NotImplementedError(
                                'Second order derivatives for spline '
                                'are not implemented yet.'
                            )

                        def _eval_is_real(self):
                            return True

                    return AmiciSplineSensitivity(
                        self.args[0],
                        self.args[1],
                        parameters[pindex],
                        *self.args[2:]
                    )

            def _eval_is_real(self):
                return True

        return AmiciSpline(self.sbmlId, self.x, *parameters)


def spline_user_functions(splines: List[AbstractSpline], p_index: Dict[sp.Symbol, int]):
    splineIds = [spline.sbmlId.name for spline in splines]
    return {
        'AmiciSpline' : [ (lambda *args : True,
            lambda splineId, x, *p : f"spl_{splineIds.index(splineId)}"
        )],
        'AmiciSplineDerivative' : [ (lambda *args : True,
            lambda splineId, x, *p : f"dspl_{splineIds.index(splineId)}"
        )],
        'AmiciSplineSensitivity' : [ (lambda *args : True,
            lambda splineId, x, paramId, *p : f"sspl_{splineIds.index(splineId)}_{p_index[paramId]}"
        )],
    }


class CubicHermiteSpline(AbstractSpline):
    def __init__(
            self,
            sbmlId: Union[str, sp.Symbol],
            x: Union[str, sp.Basic],
            xx: Sequence,
            yy: Sequence,
            dd: Sequence = None,
            *,
            bc: Optional[str] = None,
            extrapolate: Union[
                None, str,
                Tuple[Union[None, str], Union[None, str]]
            ] = None,
            logarithmic_paraterization: bool = False
        ):
        """
        Constructor for `CubicHermiteSpline` objects.

            :param sbmlId:
            The SBML ID of the parameter associated to the spline as a string or
            a SymPy symbol.

            :param x:
            The symbolic argument at which the spline is evaluated.
            It will be sympified.

            :param xx:
            The points at which the spline values are known.
            Currently they can only contain depend on constant parameters.
            It should be strictly increasing.
            It will be sympified.

            :param yy:
            The spline values at each of the points in `xx`.
            They may not depend on model species.
            It will be sympified.

            :param dd:
            The spline derivatives at each of the points in `xx`.
            They may not depend on model species.
            It will be sympified.
            If not specified, it will be computed by finite differences.

            :param bc:
            Applied boundary conditions. Can only be `'periodic'` or
            `None` if the boundary condition are not needed.
            However, if the values/derivative would lead to a spline satisfying
            periodic boundary conditions, bc is automatically set to `'periodic'`.

            :param extrapolate:
            Whether to extrapolate the spline outside the base interval
            defined by `(xx[0], xx[-1])`.
            It is a tuple of extrapolation methods, one for each side of the
            base interval.
            `None` or `'no_extrapolation'`
            means no extrapolation is to be performed
            (evaluating the spline outside the base interval is not guaranteed
            to work).
            If it is not a tuple, then the same extrapolation will be applied
            on both sides.

            :param logarithmic_paraterization:
            Interpolation is done in log scale.
        """

        if not isinstance(xx, UniformGrid):
            xx = np.asarray([sympify_noeval(x) for x in xx])
        yy = np.asarray([sympify_noeval(y) for y in yy])

        if len(xx) != len(yy):
            # NB this would be checked in AbstractSpline.__init__()
            #    however, we check it now so that an informative message
            #    can be printed (otherwise finite difference computation fails)
            raise ValueError(
                'length of xx and yy must be the same '
                f'(instead len(xx) = {len(xx)} and len(yy) = {len(yy)})'
            )

        if bc not in ('periodic', None):
            raise ValueError(
                f'unsupported bc {bc} for CubicHermiteSplines'
            )

        extrapolate = AbstractSpline._normalize_extrapolate(extrapolate, bc)

        if dd is None:
            dd = finite_differences(xx, yy, extrapolate, bc)
            self._derivatives_by_fd = True
        else:
            dd = np.asarray([sympify_noeval(d) for d in dd])
            self._derivatives_by_fd = False

        if len(xx) != len(dd):
            raise ValueError(
                'length of xx and dd must be the same '
                f'(instead len(xx) = {len(xx)} and len(dd) = {len(dd)})'
            )

        if bc is None:
            if yy[0] == yy[-1] and dd[0] == dd[-1]:
                bc = 'periodic'
        elif bc == 'periodic':
            if yy[0] != yy[-1] or dd[0] != dd[-1]:
                raise ValueError(
                    'bc=periodic but given yy and dd do not satisfy '
                    'periodic boundary conditions!'
                )

        super().__init__(
            sbmlId, x, xx, yy,
            bc=bc,
            extrapolate=extrapolate,
            logarithmic_paraterization=logarithmic_paraterization
        )

        self._dd = dd

    @property
    def dd(self) -> np.ndarray:
        "The spline derivatives at each of the points in `xx`."
        return self._dd

    @property
    def method(self) -> str:
        "Spline method (cubic Hermite spline)"
        return 'cubic_hermite'

    @property
    def derivatives_by_fd(self):
        return self.__derivatives_by_fd

    def check_if_valid(self, importer: SbmlImporter):
        """
        Check if the spline described by this object can be correctly
        be implemented by AMICI. E.g., check whether the formulas
        for spline grid points, values, ... contain species symbols.
        """
        # TODO this is very much a draft

        species = list(importer.symbols['species']['identifier'])
        for d in self.dd:
            if len(d.free_symbols.intersection(species)) != 0:
                raise ValueError('dd should not depend on model species')

        super().check_if_valid(importer)

    def d_scaled(self, i: int):
        if self.logarithmic_paraterization:
            return self.dd[i] / self.yy[i]
        else:
            return self.dd[i]

    def _poly(self, x, i) -> sp.Basic:
        """
        Return the symbolic expression for polynomial interpolant
        on the `(xx[i], xx[i+1])` interval using `x` as the spline parameter.
        """
        assert 0 <= i < len(self.xx) - 1

        dx = self.xx[i+1] - self.xx[i]
        t = (x - self.xx[i]) / dx

        h00 = 2*t**3 - 3*t**2 + 1
        h10 = t**3 - 2*t**2 + t
        h01 = -2*t**3 + 3*t**2
        h11 = t**3 - t**2

        y0 = self.y_scaled(i)
        y1 = self.y_scaled(i+1)
        dy0 = self.d_scaled(i)
        dy1 = self.d_scaled(i+1)

        with evaluate(False):
            return h00 * y0 + h10 * dx * dy0 + h01 * y1 + h11 * dx * dy1

    def _annotation_children(self) -> Dict[str, Union[str, List[str]]]:
        children = super()._annotation_children()
        if not self._derivatives_by_fd:
            children['spline_derivatives'] = [sbmlMathML(d) for d in self.dd]
        return children

    def _parameters(self):
        parameters = super()._parameters()
        for d in self.dd:
            parameters.update(d.free_symbols)
        return parameters

    def _replace_in_all_expressions(self, old: sp.Symbol, new: sp.Symbol):
        super()._replace_in_all_expressions(old, new)
        self._dd = [d.subs(old, new) for d in self.dd]

    @classmethod
    def _fromAnnotation(cls, attributes, children):
        kwargs = super()._fromAnnotation(attributes, children)

        if 'spline_derivatives' in children.keys():
            kwargs['dd'] = children.pop('spline_derivatives')

        return kwargs

    def __str__(self):
        s = f'HermiteCubicSpline on ({self.xx[0]}, {self.xx[-1]}) with {len(self.xx)} points'
        cmps = []
        if self.bc is not None:
            assert self.bc == 'periodic'
            cmps.append('periodic')
        if self.derivatives_by_fd:
            cmps.append('finite differences')
        if self.extrapolate != (None, None):
            cmps.append(f'extrapolate = {self.extrapolate}')
        if len(cmps) != 0:
            return s + ' [' + ', '.join(cmps) + ']'
        else:
            return s


def finite_differences(xx, yy, extrapolate, bc):
    dd = []

    if bc == 'periodic':
        fd = centeredFD(yy[-2], yy[0], yy[1], xx[-1] - xx[-2], xx[1] - xx[0])
    elif extrapolate[0] == 'constant':
        fd = sp.Integer(0)
    else:
        fd = onesidedFD(yy[0], yy[1], xx[1] - xx[0])
    dd.append(fd)

    for i in range(1, len(xx) - 1):
        dd.append(
            centeredFD(yy[i-1], yy[i], yy[i+1], xx[i] - xx[i-1], xx[i+1] - xx[i])
        )

    if bc == 'periodic':
        fd = dd[0]
    elif extrapolate[1] == 'constant':
        fd = sp.Integer(0)
    else:
        fd = onesidedFD(yy[-2], yy[-1], xx[-1] - xx[-2])
    dd.append(fd)

    return np.asarray(dd)


def onesidedFD(y0, y1, h):
    return sp.Mul(1 / h, y1 - y0, evaluate=False)


def centeredFD(ym1, y0, yp1, hm, hp):
    if hm == hp:
        return sp.Mul(1 / (2 * hm), yp1 - ym1, evaluate=False)
    else:
        return ((yp1 - y0) / hp + (y0 - ym1) / hm) / 2
