from __future__ import print_function
from functools import wraps
import warnings
from .export_utils import expr_to_tree, generate_pipeline_code
from deap import creator

from stopit import threading_timeoutable, TimeoutException


NUM_TESTS = 10
MAX_EVAL_SECS = 10


def _pre_test(func):
    """Check if the wrapped function works with a pretest data set.

    Reruns the wrapped function until it generates a good pipeline, for a max of
    NUM_TESTS times.

    Parameters
    ----------
    func: function
        The decorated function.

    Returns
    -------
    check_pipeline: function
        A wrapper function around the func parameter
    """
    @threading_timeoutable(default="timeout")
    def time_limited_call(func, *args):
        func(*args)

    @wraps(func)
    def check_pipeline(self, *args, **kwargs):
        bad_pipeline = True
        num_test = 0  # number of tests

        # a pool for workable pipeline
        while bad_pipeline and num_test < NUM_TESTS:
            # clone individual before each func call so it is not altered for
            # the possible next cycle loop
            args = [self._toolbox.clone(arg) if isinstance(arg, creator.Individual) else arg for arg in args]
            try:

                if func.__name__ == "_generate":
                    expr = []
                else:
                    expr = tuple(args)
                pass_gen = False
                num_test_expr = 0
                # to ensure a pipeline can be generated or mutated.
                while not pass_gen and num_test_expr < int(NUM_TESTS/2):
                    try:
                        expr = func(self, *args, **kwargs)
                        pass_gen = True
                    except:
                        num_test_expr += 1
                        pass
                # mutation operator returns tuple (ind,); crossover operator
                # returns tuple of (ind1, ind2)

                expr_tuple = expr if isinstance(expr, tuple) else (expr,)
                for expr_test in expr_tuple:
                    pipeline_code = generate_pipeline_code(
                        expr_to_tree(expr_test, self._pset),
                        self.operators
                    )
                    sklearn_pipeline = eval(pipeline_code, self.operators_context)
                    with warnings.catch_warnings():
                        warnings.simplefilter('ignore')
                        time_limited_call(
                            sklearn_pipeline.fit,
                            self.pretest_X,
                            self.pretest_y,
                            timeout=MAX_EVAL_SECS,
                        )

                    bad_pipeline = False
            except BaseException as e:
                message = '_pre_test decorator: {fname}: num_test={n} {e}.'.format(
                    n=num_test,
                    fname=func.__name__,
                    e=e

                )
                # Use the pbar output stream if it's active
                self._update_pbar(pbar_num=0, pbar_msg=message)
            finally:
                num_test += 1

        return expr


    return check_pipeline