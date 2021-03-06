from __future__ import absolute_import, division, print_function

from operator import getitem
from collections import defaultdict
from itertools import repeat
from multiprocessing import cpu_count
import numbers

import numpy as np
import dask
from dask.base import tokenize, Base
from dask.delayed import delayed
from dask.threaded import get as threaded_get
from dask.utils import derived_from
from sklearn import model_selection
from sklearn.base import is_classifier, clone, BaseEstimator, MetaEstimatorMixin
from sklearn.exceptions import NotFittedError
from sklearn.metrics.scorer import check_scoring
from sklearn.model_selection._search import _check_param_grid, BaseSearchCV
from sklearn.model_selection._split import (_BaseKFold,
                                            BaseShuffleSplit,
                                            KFold,
                                            StratifiedKFold,
                                            LeaveOneOut,
                                            LeaveOneGroupOut,
                                            LeavePOut,
                                            LeavePGroupsOut,
                                            PredefinedSplit,
                                            _CVIterableWrapper)
from sklearn.pipeline import Pipeline, FeatureUnion
from sklearn.utils.metaestimators import if_delegate_has_method
from sklearn.utils.multiclass import type_of_target
from sklearn.utils.validation import _num_samples, check_is_fitted

from ._normalize import normalize_estimator
from .methods import (fit, fit_transform, fit_and_score, pipeline, fit_best,
                      get_best_params, create_cv_results, cv_split,
                      cv_n_samples, cv_extract, cv_extract_params,
                      decompress_params, score, feature_union,
                      feature_union_concat, MISSING)
from .utils import to_indexable, to_keys, unzip

try:
    from cytoolz import get, pluck
except:  # pragma: no cover
    from toolz import get, pluck


__all__ = ['GridSearchCV', 'RandomizedSearchCV']


class TokenIterator(object):
    def __init__(self, base_token):
        self.token = base_token
        self.counts = defaultdict(int)

    def __call__(self, est):
        typ = type(est)
        c = self.counts[typ]
        self.counts[typ] += 1
        return self.token if c == 0 else self.token + str(c)


def build_graph(estimator, cv, scorer, candidate_params, X, y=None,
                groups=None, fit_params=None, iid=True, refit=True,
                error_score='raise', return_train_score=True, cache_cv=True):

    X, y, groups = to_indexable(X, y, groups)
    cv = check_cv(cv, y, is_classifier(estimator))
    # "pairwise" estimators require a different graph for CV splitting
    is_pairwise = getattr(estimator, '_pairwise', False)

    dsk = {}
    X_name, y_name, groups_name = to_keys(dsk, X, y, groups)
    n_splits = compute_n_splits(cv, X, y, groups)

    if fit_params:
        # A mapping of {name: (name, graph-key)}
        param_values = to_indexable(*fit_params.values(), allow_scalars=True)
        fit_params = {k: (k, v) for (k, v) in
                      zip(fit_params, to_keys(dsk, *param_values))}
    else:
        fit_params = {}

    fields, tokens, params = normalize_params(candidate_params)
    main_token = tokenize(normalize_estimator(estimator), fields, params,
                          X_name, y_name, groups_name, fit_params, cv,
                          error_score == 'raise', return_train_score)

    cv_name = 'cv-split-' + main_token
    dsk[cv_name] = (cv_split, cv, X_name, y_name, groups_name,
                    is_pairwise, cache_cv)

    if iid:
        weights = 'cv-n-samples-' + main_token
        dsk[weights] = (cv_n_samples, cv_name)
    else:
        weights = None

    scores = do_fit_and_score(dsk, main_token, estimator, cv_name, fields,
                              tokens, params, X_name, y_name, fit_params,
                              n_splits, error_score, scorer,
                              return_train_score)

    cv_results = 'cv-results-' + main_token
    candidate_params_name = 'cv-parameters-' + main_token
    dsk[candidate_params_name] = (decompress_params, fields, params)
    dsk[cv_results] = (create_cv_results, scores, candidate_params_name,
                       n_splits, error_score, weights)
    keys = [cv_results]

    if refit:
        best_params = 'best-params-' + main_token
        dsk[best_params] = (get_best_params, candidate_params_name, cv_results)
        best_estimator = 'best-estimator-' + main_token
        if fit_params:
            fit_params = (dict, (zip, list(fit_params.keys()),
                                list(pluck(1, fit_params.values()))))
        dsk[best_estimator] = (fit_best, clone(estimator), best_params,
                               X_name, y_name, fit_params)
        keys.append(best_estimator)

    return dsk, keys, n_splits


def normalize_params(params):
    """Take a list of dictionaries, and tokenize/normalize."""
    # Collect a set of all fields
    fields = set()
    for p in params:
        fields.update(p)
    fields = sorted(fields)

    params2 = list(pluck(fields, params, MISSING))
    # Non-basic types (including MISSING) are unique to their id
    tokens = [tuple(x if isinstance(x, (int, float, str)) else id(x)
                    for x in p) for p in params2]

    return fields, tokens, params2


def _get_fit_params(cv, fit_params, n_splits):
    if not fit_params:
        return [(n, None) for n in range(n_splits)]
    keys = []
    vals = []
    for name, (full_name, val) in fit_params.items():
        vals.append(val)
        keys.append((name, full_name))
    return [(n, (cv_extract_params, cv, keys, vals, n))
            for n in range(n_splits)]


def _group_fit_params(steps, fit_params):
    param_lk = {n: {} for n, _ in steps}
    for pname, pval in fit_params.items():
        step, param = pname.split('__', 1)
        param_lk[step][param] = pval
    return param_lk


def do_fit_and_score(dsk, main_token, est, cv, fields, tokens, params,
                     X, y, fit_params, n_splits, error_score, scorer,
                     return_train_score):
    if not isinstance(est, Pipeline):
        # Fitting and scoring can all be done as a single task
        n_and_fit_params = _get_fit_params(cv, fit_params, n_splits)

        est_type = type(est).__name__.lower()
        est_name = '%s-%s' % (est_type, main_token)
        score_name = '%s-fit-score-%s' % (est_type, main_token)
        dsk[est_name] = est

        seen = {}
        m = 0
        out = []
        out_append = out.append

        for t, p in zip(tokens, params):
            if t in seen:
                out_append(seen[t])
            else:
                for n, fit_params in n_and_fit_params:
                    dsk[(score_name, m, n)] = (fit_and_score, est_name, cv,
                                               X, y, n, scorer, error_score,
                                               fields, p, fit_params,
                                               return_train_score)
                seen[t] = (score_name, m)
                out_append((score_name, m))
                m += 1
        scores = [k + (n,) for n in range(n_splits) for k in out]
    else:
        X_train = (cv_extract, cv, X, y, True, True)
        X_test = (cv_extract, cv, X, y, True, False)
        y_train = (cv_extract, cv, X, y, False, True)
        y_test = (cv_extract, cv, X, y, False, False)

        # Fit the estimator on the training data
        X_trains = [X_train] * len(params)
        y_trains = [y_train] * len(params)
        fit_ests = do_fit(dsk, TokenIterator(main_token), est, cv,
                          fields, tokens, params, X_trains, y_trains,
                          fit_params, n_splits, error_score)

        score_name = 'score-' + main_token

        scores = []
        scores_append = scores.append
        for n in range(n_splits):
            if return_train_score:
                xtrain = X_train + (n,)
                ytrain = y_train + (n,)
            else:
                xtrain = ytrain = None

            xtest = X_test + (n,)
            ytest = y_test + (n,)

            for (name, m) in fit_ests:
                dsk[(score_name, m, n)] = (score, (name, m, n),
                                        xtest, ytest, xtrain, ytrain, scorer)
                scores_append((score_name, m, n))
    return scores


def do_fit(dsk, next_token, est, cv, fields, tokens, params, Xs, ys,
           fit_params, n_splits, error_score):
    if isinstance(est, Pipeline) and params is not None:
        return _do_pipeline(dsk, next_token, est, cv, fields, tokens, params,
                            Xs, ys, fit_params, n_splits, error_score, False)
    else:
        n_and_fit_params = _get_fit_params(cv, fit_params, n_splits)

        if params is None:
            params = tokens = repeat(None)
            fields = None

        token = next_token(est)
        est_type = type(est).__name__.lower()
        est_name = '%s-%s' % (est_type, token)
        fit_name = '%s-fit-%s' % (est_type, token)
        dsk[est_name] = est

        seen = {}
        m = 0
        out = []
        out_append = out.append

        for X, y, t, p in zip(Xs, ys, tokens, params):
            if (X, y, t) in seen:
                out_append(seen[X, y, t])
            else:
                for n, fit_params in n_and_fit_params:
                    dsk[(fit_name, m, n)] = (fit, est_name, X + (n,),
                                             y + (n,), error_score,
                                             fields, p, fit_params)
                seen[(X, y, t)] = (fit_name, m)
                out_append((fit_name, m))
                m += 1

        return out


def do_fit_transform(dsk, next_token, est, cv, fields, tokens, params, Xs, ys,
                     fit_params, n_splits, error_score):
    if isinstance(est, Pipeline) and params is not None:
        return _do_pipeline(dsk, next_token, est, cv, fields, tokens, params,
                            Xs, ys, fit_params, n_splits, error_score, True)
    elif isinstance(est, FeatureUnion) and params is not None:
        return _do_featureunion(dsk, next_token, est, cv, fields, tokens,
                                params, Xs, ys, fit_params, n_splits,
                                error_score)
    else:
        n_and_fit_params = _get_fit_params(cv, fit_params, n_splits)

        if params is None:
            params = tokens = repeat(None)
            fields = None

        name = type(est).__name__.lower()
        token = next_token(est)
        fit_Xt_name = '%s-fit-transform-%s' % (name, token)
        fit_name = '%s-fit-%s' % (name, token)
        Xt_name = '%s-transform-%s' % (name, token)
        est_name = '%s-%s' % (type(est).__name__.lower(), token)
        dsk[est_name] = est

        seen = {}
        m = 0
        out = []
        out_append = out.append

        for X, y, t, p in zip(Xs, ys, tokens, params):
            if (X, y, t) in seen:
                out_append(seen[X, y, t])
            else:
                for n, fit_params in n_and_fit_params:
                    dsk[(fit_Xt_name, m, n)] = (fit_transform, est_name,
                                                X + (n,), y + (n,),
                                                error_score, fields, p,
                                                fit_params)
                    dsk[(fit_name, m, n)] = (getitem, (fit_Xt_name, m, n), 0)
                    dsk[(Xt_name, m, n)] = (getitem, (fit_Xt_name, m, n), 1)
                seen[X, y, t] = m
                out_append(m)
                m += 1

        return [(fit_name, i) for i in out], [(Xt_name, i) for i in out]


def _group_subparams(steps, fields, ignore=()):
    # Group the fields into a mapping of {stepname: [(newname, orig_index)]}
    field_to_index = dict(zip(fields, range(len(fields))))
    step_fields_lk = {s: [] for s, _ in steps}
    for f in fields:
        if '__' in f:
            step, param = f.split('__', 1)
            if step in step_fields_lk:
                step_fields_lk[step].append((param, field_to_index[f]))
                continue
        if f not in step_fields_lk and f not in ignore:
            raise ValueError("Unknown parameter: `%s`" % f)
    return field_to_index, step_fields_lk


def _group_ids_by_index(index, tokens):
    id_groups = []

    def new_group():
        o = []
        id_groups.append(o)
        return o.append

    _id_groups = defaultdict(new_group)
    for n, t in enumerate(pluck(index, tokens)):
        _id_groups[t](n)
    return id_groups


def _do_fit_step(dsk, next_token, step, cv, fields, tokens, params, Xs, ys,
                 fit_params, n_splits, error_score, step_fields_lk,
                 fit_params_lk, field_to_index, step_name, none_passthrough,
                 is_transform):
    sub_fields, sub_inds = map(list, unzip(step_fields_lk[step_name], 2))
    sub_fit_params = fit_params_lk[step_name]

    if step_name in field_to_index:
        # The estimator may change each call
        new_fits = {}
        new_Xs = {}
        est_index = field_to_index[step_name]

        for ids in _group_ids_by_index(est_index, tokens):
            # Get the estimator for this subgroup
            sub_est = params[ids[0]][est_index]
            if sub_est is MISSING:
                sub_est = step

            # If an estimator is `None`, there's nothing to do
            if sub_est is None:
                nones = dict.fromkeys(ids, None)
                new_fits.update(nones)
                if is_transform:
                    if none_passthrough:
                        new_Xs.update(zip(ids, get(ids, Xs)))
                    else:
                        new_Xs.update(nones)
            else:
                # Extract the proper subset of Xs, ys
                sub_Xs = get(ids, Xs)
                sub_ys = get(ids, ys)
                # Only subset the parameters/tokens if necessary
                if sub_fields:
                    sub_tokens = list(pluck(sub_inds, get(ids, tokens)))
                    sub_params = list(pluck(sub_inds, get(ids, params)))
                else:
                    sub_tokens = sub_params = None

                if is_transform:
                    sub_fits, sub_Xs = do_fit_transform(dsk, next_token,
                                                        sub_est, cv, sub_fields,
                                                        sub_tokens, sub_params,
                                                        sub_Xs, sub_ys,
                                                        sub_fit_params,
                                                        n_splits, error_score)
                    new_Xs.update(zip(ids, sub_Xs))
                    new_fits.update(zip(ids, sub_fits))
                else:
                    sub_fits = do_fit(dsk, next_token, sub_est, cv,
                                        sub_fields, sub_tokens, sub_params,
                                        sub_Xs, sub_ys, sub_fit_params,
                                        n_splits, error_score)
                    new_fits.update(zip(ids, sub_fits))
        # Extract lists of transformed Xs and fit steps
        all_ids = list(range(len(Xs)))
        if is_transform:
            Xs = get(all_ids, new_Xs)
        fits = get(all_ids, new_fits)
    elif step is None:
        # Nothing to do
        fits = [None] * len(Xs)
        if not none_passthrough:
            Xs = fits
    else:
        # Only subset the parameters/tokens if necessary
        if sub_fields:
            sub_tokens = list(pluck(sub_inds, tokens))
            sub_params = list(pluck(sub_inds, params))
        else:
            sub_tokens = sub_params = None

        if is_transform:
            fits, Xs = do_fit_transform(dsk, next_token, step, cv,
                                        sub_fields, sub_tokens, sub_params,
                                        Xs, ys, sub_fit_params, n_splits,
                                        error_score)
        else:
            fits = do_fit(dsk, next_token, step, cv, sub_fields,
                            sub_tokens, sub_params, Xs, ys, sub_fit_params,
                            n_splits, error_score)
    return (fits, Xs) if is_transform else (fits, None)


def _do_pipeline(dsk, next_token, est, cv, fields, tokens, params, Xs, ys,
                 fit_params, n_splits, error_score, is_transform):
    if 'steps' in fields:
        raise NotImplementedError("Setting Pipeline.steps in a gridsearch")

    field_to_index, step_fields_lk = _group_subparams(est.steps, fields)
    fit_params_lk = _group_fit_params(est.steps, fit_params)

    # A list of (step, is_transform)
    instrs = [(s, True) for s in est.steps[:-1]]
    instrs.append((est.steps[-1], is_transform))

    fit_steps = []
    for (step_name, step), transform in instrs:
        fits, Xs = _do_fit_step(dsk, next_token, step, cv, fields, tokens,
                                params, Xs, ys, fit_params, n_splits,
                                error_score, step_fields_lk, fit_params_lk,
                                field_to_index, step_name, True, transform)
        fit_steps.append(fits)

    # Rebuild the pipelines
    step_names = [n for n, _ in est.steps]
    out_ests = []
    out_ests_append = out_ests.append
    name = 'pipeline-' + next_token(est)
    m = 0
    seen = {}
    for steps in zip(*fit_steps):
        if steps in seen:
            out_ests_append(seen[steps])
        else:
            for n in range(n_splits):
                dsk[(name, m, n)] = (pipeline, step_names,
                                     [None if s is None else s + (n,)
                                      for s in steps])
            seen[steps] = (name, m)
            out_ests_append((name, m))
            m += 1

    if is_transform:
        return out_ests, Xs
    return out_ests


def _do_n_samples(dsk, token, Xs, n_splits):
    name = 'n_samples-' + token
    n_samples = []
    n_samples_append = n_samples.append
    seen = {}
    m = 0
    for x in Xs:
        if x in seen:
            n_samples_append(seen[x])
        else:
            for n in range(n_splits):
                dsk[name, m, n] = (_num_samples, x + (n,))
            n_samples_append((name, m))
            seen[x] = (name, m)
            m += 1
    return n_samples


def _do_featureunion(dsk, next_token, est, cv, fields, tokens, params, Xs, ys,
                     fit_params, n_splits, error_score):
    if 'transformer_list' in fields:
        raise NotImplementedError("Setting FeatureUnion.transformer_list "
                                  "in a gridsearch")

    (field_to_index,
     step_fields_lk) = _group_subparams(est.transformer_list, fields,
                                        ignore=('transformer_weights'))
    fit_params_lk = _group_fit_params(est.transformer_list, fit_params)

    token = next_token(est)

    n_samples = _do_n_samples(dsk, token, Xs, n_splits)

    fit_steps = []
    tr_Xs = []
    for (step_name, step) in est.transformer_list:
        fits, out_Xs = _do_fit_step(dsk, next_token, step, cv, fields, tokens,
                                    params, Xs, ys, fit_params, n_splits,
                                    error_score, step_fields_lk, fit_params_lk,
                                    field_to_index, step_name, False, True)
        fit_steps.append(fits)
        tr_Xs.append(out_Xs)

    # Rebuild the FeatureUnions
    step_names = [n for n, _ in est.transformer_list]

    if 'transformer_weights' in field_to_index:
        index = field_to_index['transformer_weights']
        weight_lk = {}
        weight_tokens = list(pluck(index, tokens))
        for i, tok in enumerate(weight_tokens):
            if tok not in weight_lk:
                weights = params[i][index]
                if weights is MISSING:
                    weights = est.transformer_weights
                lk = weights or {}
                weight_list = [lk.get(n) for n in step_names]
                weight_lk[tok] = (weights, weight_list)
        weights = get(weight_tokens, weight_lk)
    else:
        lk = est.transformer_weights or {}
        weight_list = [lk.get(n) for n in step_names]
        weight_tokens = repeat(None)
        weights = repeat((est.transformer_weights, weight_list))

    out = []
    out_append = out.append
    fit_name = 'feature-union-' + token
    tr_name = 'feature-union-concat-' + token
    m = 0
    seen = {}
    for steps, Xs, wt, (w, wl), nsamp in zip(zip(*fit_steps), zip(*tr_Xs),
                                             weight_tokens, weights, n_samples):
        if (steps, wt) in seen:
            out_append(seen[steps, wt])
        else:
            for n in range(n_splits):
                dsk[(fit_name, m, n)] = (feature_union, step_names,
                                         [None if s is None else s + (n,)
                                          for s in steps], w)
                dsk[(tr_name, m, n)] = (feature_union_concat,
                                        [None if x is None else x + (n,)
                                         for x in Xs], nsamp + (n,), wl)
            seen[steps, wt] = m
            out_append(m)
            m += 1
    return [(fit_name, i) for i in out], [(tr_name, i) for i in out]


# ------------ #
# CV splitting #
# ------------ #

def check_cv(cv=3, y=None, classifier=False):
    """Dask aware version of ``sklearn.model_selection.check_cv``

    Same as the scikit-learn version, but works if ``y`` is a dask object.
    """
    if cv is None:
        cv = 3

    # If ``cv`` is not an integer, the scikit-learn implementation doesn't
    # touch the ``y`` object, so passing on a dask object is fine
    if not isinstance(y, Base) or not isinstance(cv, numbers.Integral):
        return model_selection.check_cv(cv, y, classifier)

    if classifier:
        # ``y`` is a dask object. We need to compute the target type
        target_type = delayed(type_of_target, pure=True)(y).compute()
        if target_type in ('binary', 'multiclass'):
            return StratifiedKFold(cv)
    return KFold(cv)


def compute_n_splits(cv, X, y=None, groups=None):
    """Return the number of splits.

    Parameters
    ----------
    cv : BaseCrossValidator
    X, y, groups : array_like, dask object, or None

    Returns
    -------
    n_splits : int
    """
    if not any(isinstance(i, Base) for i in (X, y, groups)):
        return cv.get_n_splits(X, y, groups)

    if isinstance(cv, (_BaseKFold, BaseShuffleSplit)):
        return cv.n_splits

    elif isinstance(cv, PredefinedSplit):
        return len(cv.unique_folds)

    elif isinstance(cv, _CVIterableWrapper):
        return len(cv.cv)

    elif isinstance(cv, (LeaveOneOut, LeavePOut)) and not isinstance(X, Base):
        # Only `X` is referenced for these classes
        return cv.get_n_splits(X, None, None)

    elif (isinstance(cv, (LeaveOneGroupOut, LeavePGroupsOut)) and not
          isinstance(groups, Base)):
        # Only `groups` is referenced for these classes
        return cv.get_n_splits(None, None, groups)

    else:
        return delayed(cv).get_n_splits(X, y, groups).compute()


def _normalize_n_jobs(n_jobs):
    if not isinstance(n_jobs, int):
        raise TypeError("n_jobs should be an int, got %s" % n_jobs)
    if n_jobs == -1:
        n_jobs = None  # Scheduler default is use all cores
    elif n_jobs < -1:
        n_jobs = cpu_count() + 1 + n_jobs
    return n_jobs


_scheduler_aliases = {'sync': 'synchronous',
                      'sequential': 'synchronous',
                      'threaded': 'threading'}


def _normalize_scheduler(scheduler, n_jobs, loop=None):
    # Default
    if scheduler is None:
        scheduler = dask.context._globals.get('get')
        if scheduler is None:
            scheduler = dask.get if n_jobs == 1 else threaded_get
        return scheduler

    # Get-functions
    if callable(scheduler):
        return scheduler

    # Support name aliases
    if isinstance(scheduler, str):
        scheduler = _scheduler_aliases.get(scheduler, scheduler)

    if scheduler in ('threading', 'multiprocessing') and n_jobs == 1:
        scheduler = dask.get
    elif scheduler == 'threading':
        scheduler = threaded_get
    elif scheduler == 'multiprocessing':
        from dask.multiprocessing import get as scheduler
    elif scheduler == 'synchronous':
        scheduler = dask.get
    else:
        try:
            from dask.distributed import Client
            # We pass loop to make testing possible, not needed for normal use
            return Client(scheduler, set_as_default=False, loop=loop).get
        except Exception as e:
            msg = ("Failed to initialize scheduler from parameter %r. "
                   "This could be due to a typo, or a failure to initialize "
                   "the distributed scheduler. Original error is below:\n\n"
                   "%r" % (scheduler, e))
        # Re-raise outside the except to provide a cleaner error message
        raise ValueError(msg)
    return scheduler


class DaskBaseSearchCV(BaseEstimator, MetaEstimatorMixin):
    """Base class for hyper parameter search with cross-validation."""

    def __init__(self, estimator, scoring=None, iid=True, refit=True, cv=None,
                 error_score='raise', return_train_score=True, scheduler=None,
                 n_jobs=-1, cache_cv=True):
        self.scoring = scoring
        self.estimator = estimator
        self.iid = iid
        self.refit = refit
        self.cv = cv
        self.error_score = error_score
        self.return_train_score = return_train_score
        self.scheduler = scheduler
        self.n_jobs = n_jobs
        self.cache_cv = cache_cv

    @property
    def _estimator_type(self):
        return self.estimator._estimator_type

    @property
    def best_params_(self):
        check_is_fitted(self, 'cv_results_')
        return self.cv_results_['params'][self.best_index_]

    @property
    def best_score_(self):
        check_is_fitted(self, 'cv_results_')
        return self.cv_results_['mean_test_score'][self.best_index_]

    def _check_is_fitted(self, method_name):
        if not self.refit:
            msg = ('This {0} instance was initialized with refit=False. {1} '
                   'is available only after refitting on the best '
                   'parameters.').format(type(self).__name__, method_name)
            raise NotFittedError(msg)
        else:
            check_is_fitted(self, 'best_estimator_')

    @property
    def classes_(self):
        self._check_is_fitted("classes_")
        return self.best_estimator_.classes_

    @if_delegate_has_method(delegate=('best_estimator_', 'estimator'))
    @derived_from(BaseSearchCV)
    def predict(self, X):
        self._check_is_fitted('predict')
        return self.best_estimator_.predict(X)

    @if_delegate_has_method(delegate=('best_estimator_', 'estimator'))
    @derived_from(BaseSearchCV)
    def predict_proba(self, X):
        self._check_is_fitted('predict_proba')
        return self.best_estimator_.predict_proba(X)

    @if_delegate_has_method(delegate=('best_estimator_', 'estimator'))
    @derived_from(BaseSearchCV)
    def predict_log_proba(self, X):
        self._check_is_fitted('predict_log_proba')
        return self.best_estimator_.predict_log_proba(X)

    @if_delegate_has_method(delegate=('best_estimator_', 'estimator'))
    @derived_from(BaseSearchCV)
    def decision_function(self, X):
        self._check_is_fitted('decision_function')
        return self.best_estimator_.decision_function(X)

    @if_delegate_has_method(delegate=('best_estimator_', 'estimator'))
    @derived_from(BaseSearchCV)
    def transform(self, X):
        self._check_is_fitted('transform')
        return self.best_estimator_.transform(X)

    @if_delegate_has_method(delegate=('best_estimator_', 'estimator'))
    @derived_from(BaseSearchCV)
    def inverse_transform(self, Xt):
        self._check_is_fitted('inverse_transform')
        return self.best_estimator_.transform(Xt)

    @derived_from(BaseSearchCV)
    def score(self, X, y=None):
        if self.scorer_ is None:
            raise ValueError("No score function explicitly defined, "
                             "and the estimator doesn't provide one %s"
                             % self.best_estimator_)
        return self.scorer_(self.best_estimator_, X, y)

    def fit(self, X, y=None, groups=None, **fit_params):
        """Run fit with all sets of parameters.

        Parameters
        ----------
        X : array-like, shape = [n_samples, n_features]
            Training vector, where n_samples is the number of samples and
            n_features is the number of features.
        y : array-like, shape = [n_samples] or [n_samples, n_output], optional
            Target relative to X for classification or regression;
            None for unsupervised learning.
        groups : array-like, shape = [n_samples], optional
            Group labels for the samples used while splitting the dataset into
            train/test set.
        **fit_params
            Parameters passed to the ``fit`` method of the estimator
        """
        estimator = self.estimator
        self.scorer_ = check_scoring(estimator, scoring=self.scoring)
        error_score = self.error_score
        if not (isinstance(error_score, numbers.Number) or
                error_score == 'raise'):
            raise ValueError("error_score must be the string 'raise' or a"
                             " numeric value.")

        dsk, keys, n_splits = build_graph(estimator, self.cv, self.scorer_,
                                          list(self._get_param_iterator()),
                                          X, y, groups, fit_params,
                                          iid=self.iid,
                                          refit=self.refit,
                                          error_score=error_score,
                                          return_train_score=self.return_train_score,
                                          cache_cv=self.cache_cv)
        self.dask_graph_ = dsk
        self.n_splits_ = n_splits

        n_jobs = _normalize_n_jobs(self.n_jobs)
        scheduler = _normalize_scheduler(self.scheduler, n_jobs)

        out = scheduler(dsk, keys, num_workers=n_jobs)

        self.cv_results_ = results = out[0]
        self.best_index_ = np.flatnonzero(results["rank_test_score"] == 1)[0]

        if self.refit:
            self.best_estimator_ = out[1]
        return self

    def visualize(self, filename='mydask', format=None, **kwargs):
        """Render the task graph for this parameter search using ``graphviz``.

        Requires ``graphviz`` to be installed.

        Parameters
        ----------
        filename : str or None, optional
            The name (without an extension) of the file to write to disk.  If
            `filename` is None, no file will be written, and we communicate
            with dot using only pipes.
        format : {'png', 'pdf', 'dot', 'svg', 'jpeg', 'jpg'}, optional
            Format in which to write output file.  Default is 'png'.
        **kwargs
            Additional keyword arguments to forward to ``dask.dot.to_graphviz``.

        Returns
        -------
        result : IPython.diplay.Image, IPython.display.SVG, or None
            See ``dask.dot.dot_graph`` for more information.
        """
        check_is_fitted(self, 'dask_graph_')
        return dask.visualize(self.dask_graph_, filename=filename,
                              format=format, **kwargs)


_DOC_TEMPLATE = """{oneliner}

{name} implements a "fit" and a "score" method.
It also implements "predict", "predict_proba", "decision_function",
"transform" and "inverse_transform" if they are implemented in the
estimator used.

{description}

Parameters
----------
estimator : estimator object.
    This is assumed to implement the scikit-learn estimator interface.
    Either estimator needs to provide a ``score`` function,
    or ``scoring`` must be passed.

{parameters}

scoring : string, callable or None, default=None
    A string (see model evaluation documentation) or
    a scorer callable object / function with signature
    ``scorer(estimator, X, y)``.
    If ``None``, the ``score`` method of the estimator is used.

iid : boolean, default=True
    If True, the data is assumed to be identically distributed across
    the folds, and the loss minimized is the total loss per sample,
    and not the mean loss across the folds.

cv : int, cross-validation generator or an iterable, optional
    Determines the cross-validation splitting strategy.
    Possible inputs for cv are:
        - None, to use the default 3-fold cross validation,
        - integer, to specify the number of folds in a ``(Stratified)KFold``,
        - An object to be used as a cross-validation generator.
        - An iterable yielding train, test splits.

    For integer/None inputs, if the estimator is a classifier and ``y`` is
    either binary or multiclass, ``StratifiedKFold`` is used. In all
    other cases, ``KFold`` is used.

refit : boolean, default=True
    Refit the best estimator with the entire dataset.
    If "False", it is impossible to make predictions using
    this {name} instance after fitting.

error_score : 'raise' (default) or numeric
    Value to assign to the score if an error occurs in estimator fitting.
    If set to 'raise', the error is raised. If a numeric value is given,
    FitFailedWarning is raised. This parameter does not affect the refit
    step, which will always raise the error.

return_train_score : boolean, default=True
    If ``'False'``, the ``cv_results_`` attribute will not include training
    scores.

scheduler : string, callable, or None, default=None
    The dask scheduler to use. Default is to use the global scheduler if set,
    and fallback to the threaded scheduler otherwise. To use a different
    scheduler, specify it by name (either "threading", "multiprocessing",
    or "synchronous") or provide the scheduler ``get`` function. Other
    arguments are assumed to be the address of a distributed scheduler,
    and passed to ``dask.distributed.Client``.

n_jobs : int, default=-1
    Number of jobs to run in parallel. Ignored for the synchronous and
    distributed schedulers. If ``n_jobs == -1`` [default] all cpus are used.
    For ``n_jobs < -1``, ``(n_cpus + 1 + n_jobs)`` are used.

cache_cv : bool, default=True
    Whether to extract each train/test subset at most once in each worker
    process, or every time that subset is needed. Caching the splits can
    speedup computation at the cost of increased memory usage per worker
    process.

    If True, worst case memory usage is ``(n_splits + 1) * (X.nbytes +
    y.nbytes)`` per worker. If False, worst case memory usage is
    ``(n_threads_per_worker + 1) * (X.nbytes + y.nbytes)`` per worker.

Examples
--------
{example}

Attributes
----------
cv_results_ : dict of numpy (masked) ndarrays
    A dict with keys as column headers and values as columns, that can be
    imported into a pandas ``DataFrame``.

    For instance the below given table

    +------------+-----------+------------+-----------------+---+---------+
    |param_kernel|param_gamma|param_degree|split0_test_score|...|rank.....|
    +============+===========+============+=================+===+=========+
    |  'poly'    |     --    |      2     |        0.8      |...|    2    |
    +------------+-----------+------------+-----------------+---+---------+
    |  'poly'    |     --    |      3     |        0.7      |...|    4    |
    +------------+-----------+------------+-----------------+---+---------+
    |  'rbf'     |     0.1   |     --     |        0.8      |...|    3    |
    +------------+-----------+------------+-----------------+---+---------+
    |  'rbf'     |     0.2   |     --     |        0.9      |...|    1    |
    +------------+-----------+------------+-----------------+---+---------+

    will be represented by a ``cv_results_`` dict of::

        {{
        'param_kernel': masked_array(data = ['poly', 'poly', 'rbf', 'rbf'],
                                        mask = [False False False False]...)
        'param_gamma': masked_array(data = [-- -- 0.1 0.2],
                                    mask = [ True  True False False]...),
        'param_degree': masked_array(data = [2.0 3.0 -- --],
                                        mask = [False False  True  True]...),
        'split0_test_score'  : [0.8, 0.7, 0.8, 0.9],
        'split1_test_score'  : [0.82, 0.5, 0.7, 0.78],
        'mean_test_score'    : [0.81, 0.60, 0.75, 0.82],
        'std_test_score'     : [0.02, 0.01, 0.03, 0.03],
        'rank_test_score'    : [2, 4, 3, 1],
        'split0_train_score' : [0.8, 0.9, 0.7],
        'split1_train_score' : [0.82, 0.5, 0.7],
        'mean_train_score'   : [0.81, 0.7, 0.7],
        'std_train_score'    : [0.03, 0.03, 0.04],
        'params'             : [{{'kernel': 'poly', 'degree': 2}}, ...],
        }}

    NOTE that the key ``'params'`` is used to store a list of parameter
    settings dict for all the parameter candidates.

best_estimator_ : estimator
    Estimator that was chosen by the search, i.e. estimator
    which gave highest score (or smallest loss if specified)
    on the left out data. Not available if refit=False.

best_score_ : float
    Score of best_estimator on the left out data.

best_params_ : dict
    Parameter setting that gave the best results on the hold out data.

best_index_ : int
    The index (of the ``cv_results_`` arrays) which corresponds to the best
    candidate parameter setting.

    The dict at ``search.cv_results_['params'][search.best_index_]`` gives
    the parameter setting for the best model, that gives the highest
    mean score (``search.best_score_``).

scorer_ : function
    Scorer function used on the held out data to choose the best
    parameters for the model.

n_splits_ : int
    The number of cross-validation splits (folds/iterations).

Notes
------
The parameters selected are those that maximize the score of the left out
data, unless an explicit score is passed in which case it is used instead.
"""

# ------------ #
# GridSearchCV #
# ------------ #

_grid_oneliner = """\
Exhaustive search over specified parameter values for an estimator.\
"""
_grid_description = """\
The parameters of the estimator used to apply these methods are optimized
by cross-validated grid-search over a parameter grid.\
"""
_grid_parameters = """\
param_grid : dict or list of dictionaries
    Dictionary with parameters names (string) as keys and lists of
    parameter settings to try as values, or a list of such
    dictionaries, in which case the grids spanned by each dictionary
    in the list are explored. This enables searching over any sequence
    of parameter settings.\
"""
_grid_example = """\
>>> import dask_searchcv as dcv
>>> from sklearn import svm, datasets
>>> iris = datasets.load_iris()
>>> parameters = {'kernel': ['linear', 'rbf'], 'C': [1, 10]}
>>> svc = svm.SVC()
>>> clf = dcv.GridSearchCV(svc, parameters)
>>> clf.fit(iris.data, iris.target)  # doctest: +NORMALIZE_WHITESPACE +ELLIPSIS
GridSearchCV(cache_cv=..., cv=..., error_score=...,
        estimator=SVC(C=..., cache_size=..., class_weight=..., coef0=...,
                      decision_function_shape=..., degree=..., gamma=...,
                      kernel=..., max_iter=-1, probability=False,
                      random_state=..., shrinking=..., tol=...,
                      verbose=...),
        iid=..., n_jobs=..., param_grid=..., refit=..., return_train_score=...,
        scheduler=..., scoring=...)
>>> sorted(clf.cv_results_.keys())  # doctest: +NORMALIZE_WHITESPACE +ELLIPSIS
['mean_test_score', 'mean_train_score', 'param_C', 'param_kernel',...
 'params', 'rank_test_score', 'split0_test_score', 'split0_train_score',...
 'split1_test_score', 'split1_train_score', 'split2_test_score',...
 'split2_train_score', 'std_test_score', 'std_train_score'...]\
"""


class GridSearchCV(DaskBaseSearchCV):
    __doc__ = _DOC_TEMPLATE.format(name="GridSearchCV",
                                   oneliner=_grid_oneliner,
                                   description=_grid_description,
                                   parameters=_grid_parameters,
                                   example=_grid_example)

    def __init__(self, estimator, param_grid, scoring=None, iid=True,
                 refit=True, cv=None, error_score='raise',
                 return_train_score=True, scheduler=None, n_jobs=-1,
                 cache_cv=True):
        super(GridSearchCV, self).__init__(estimator=estimator,
                scoring=scoring, iid=iid, refit=refit, cv=cv,
                error_score=error_score, return_train_score=return_train_score,
                scheduler=scheduler, n_jobs=n_jobs, cache_cv=cache_cv)

        _check_param_grid(param_grid)
        self.param_grid = param_grid

    def _get_param_iterator(self):
        """Return ParameterGrid instance for the given param_grid"""
        return model_selection.ParameterGrid(self.param_grid)


# ------------------ #
# RandomizedSearchCV #
# ------------------ #

_randomized_oneliner = "Randomized search on hyper parameters."
_randomized_description = """\
In contrast to GridSearchCV, not all parameter values are tried out, but
rather a fixed number of parameter settings is sampled from the specified
distributions. The number of parameter settings that are tried is
given by n_iter.

If all parameters are presented as a list, sampling without replacement is
performed. If at least one parameter is given as a distribution, sampling
with replacement is used. It is highly recommended to use continuous
distributions for continuous parameters.\
"""
_randomized_parameters = """\
param_distributions : dict
    Dictionary with parameters names (string) as keys and distributions
    or lists of parameters to try. Distributions must provide a ``rvs``
    method for sampling (such as those from scipy.stats.distributions).
    If a list is given, it is sampled uniformly.

n_iter : int, default=10
    Number of parameter settings that are sampled. n_iter trades
    off runtime vs quality of the solution.

random_state : int or RandomState
    Pseudo random number generator state used for random uniform sampling
    from lists of possible values instead of scipy.stats distributions.\
"""
_randomized_example = """\
>>> import dask_searchcv as dcv
>>> from scipy.stats import expon
>>> from sklearn import svm, datasets
>>> iris = datasets.load_iris()
>>> parameters = {'C': expon(scale=100), 'kernel': ['linear', 'rbf']}
>>> svc = svm.SVC()
>>> clf = dcv.RandomizedSearchCV(svc, parameters, n_iter=100)
>>> clf.fit(iris.data, iris.target)  # doctest: +NORMALIZE_WHITESPACE +ELLIPSIS
RandomizedSearchCV(cache_cv=..., cv=..., error_score=...,
        estimator=SVC(C=..., cache_size=..., class_weight=..., coef0=...,
                      decision_function_shape=..., degree=..., gamma=...,
                      kernel=..., max_iter=..., probability=...,
                      random_state=..., shrinking=..., tol=...,
                      verbose=...),
        iid=..., n_iter=..., n_jobs=..., param_distributions=...,
        random_state=..., refit=..., return_train_score=...,
        scheduler=..., scoring=...)
>>> sorted(clf.cv_results_.keys())  # doctest: +NORMALIZE_WHITESPACE +ELLIPSIS
['mean_test_score', 'mean_train_score', 'param_C', 'param_kernel',...
 'params', 'rank_test_score', 'split0_test_score', 'split0_train_score',...
 'split1_test_score', 'split1_train_score', 'split2_test_score',...
 'split2_train_score', 'std_test_score', 'std_train_score'...]\
"""


class RandomizedSearchCV(DaskBaseSearchCV):
    __doc__ = _DOC_TEMPLATE.format(name="RandomizedSearchCV",
                                   oneliner=_randomized_oneliner,
                                   description=_randomized_description,
                                   parameters=_randomized_parameters,
                                   example=_randomized_example)

    def __init__(self, estimator, param_distributions, n_iter=10,
                 random_state=None, scoring=None, iid=True, refit=True,
                 cv=None, error_score='raise', return_train_score=True,
                 scheduler=None, n_jobs=-1, cache_cv=True):

        super(RandomizedSearchCV, self).__init__(estimator=estimator,
                scoring=scoring, iid=iid, refit=refit, cv=cv,
                error_score=error_score, return_train_score=return_train_score,
                scheduler=scheduler, n_jobs=n_jobs, cache_cv=cache_cv)

        self.param_distributions = param_distributions
        self.n_iter = n_iter
        self.random_state = random_state

    def _get_param_iterator(self):
        """Return ParameterSampler instance for the given distributions"""
        return model_selection.ParameterSampler(self.param_distributions,
                self.n_iter, random_state=self.random_state)
