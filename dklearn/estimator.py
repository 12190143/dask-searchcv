from __future__ import absolute_import, print_function, division

from operator import getitem

from sklearn.base import clone, BaseEstimator
from dask.base import tokenize
from dask.delayed import Delayed
from toolz import merge

from .core import DaskBaseEstimator, unpack_arguments, from_sklearn


def _fit(est, X, y, kwargs):
    return clone(est).fit(X, y, **kwargs)


def _predict(est, X):
    return est.predict(X)


def _score(est, X, y, kwargs):
    return est.score(X, y, **kwargs)


def _transform(est, X):
    return est.transform(X)


def _fit_transform(est, X, y, kwargs):
    est = clone(est)
    if hasattr(est, 'fit_transform'):
        fit = est
        tr = est.fit_transform(X, y, **kwargs)
    else:
        fit = est.fit(X, y, **kwargs)
        tr = est.transform(X)
    return fit, tr


class Estimator(DaskBaseEstimator):
    """A class for wrapping a scikit-learn estimator.

    All operations done on this estimator are pure (no mutation), and are done
    lazily (if applicable). Calling `compute` results in the wrapped estimator.
    """

    def __init__(self, est, dask=None, name=None):
        if dask is None and name is None:
            name = 'from_sklearn-' + tokenize(est)
            dask = {name: est}
        elif dask is None or name is None:
            raise ValueError("Must provide both dask and name")
        self.dask = dask
        self._name = name
        self._est = est

    @staticmethod
    def _finalize(res):
        return res[0]

    def _keys(self):
        return [self._name]

    @classmethod
    def from_sklearn(cls, est):
        """Wrap a scikit-learn estimator"""
        return cls(est)

    @property
    def _estimator_type(self):
        return self._est._estimator_type

    def fit(self, X, y, **kwargs):
        name = 'fit-' + tokenize(self, X, y, kwargs)
        X, y, dsk = unpack_arguments(X, y)
        dsk.update(self.dask)
        dsk[name] = (_fit, self._name, X, y, kwargs)
        return Estimator(self._est, dsk, name)

    def predict(self, X):
        name = 'predict-' + tokenize(self, X)
        X, dsk = unpack_arguments(X)
        dsk[name] = (_predict, self._name, X)
        return Delayed(name, [dsk, self.dask])

    def score(self, X, y, **kwargs):
        name = 'score-' + tokenize(self, X, y, kwargs)
        X, y, dsk = unpack_arguments(X, y)
        dsk[name] = (_score, self._name, X, y, kwargs)
        return Delayed(name, [dsk, self.dask])

    def transform(self, X):
        name = 'transform-' + tokenize(self, X)
        X, dsk = unpack_arguments(X)
        dsk[name] = (_transform, self._name, X)
        return Delayed(name, [dsk, self.dask])

    def _fit_transform(self, X, y, **kwargs):
        token = tokenize(self, X, y, kwargs)
        fit_tr_name = 'fit-transform-' + token
        fit_name = 'fit-' + token
        tr_name = 'tr-' + token
        X, y, dsk = unpack_arguments(X, y)
        dsk[fit_tr_name] = (_fit_transform, self._name, X, y, kwargs)
        dsk1 = merge({fit_name: (getitem, fit_tr_name, 0)}, dsk, self.dask)
        dsk2 = merge({tr_name: (getitem, fit_tr_name, 1)}, dsk, self.dask)
        return Estimator(self._est, dsk1, fit_name), Delayed(tr_name, [dsk2])


from_sklearn.dispatch.register(BaseEstimator, Estimator.from_sklearn)
