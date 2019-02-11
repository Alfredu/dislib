from math import sqrt
import sys
from time import time

import numpy as np
from pycompss.api.api import compss_wait_on
from pycompss.api.parameter import INOUT
from pycompss.api.task import task
from scipy import sparse
from sklearn.metrics import mean_squared_error

shallow_tracing = True

class ALS(object):
    def __init__(self, seed=None, n_f=100, lambda_=0.065,
                 convergence_threshold=0.0001 ** 2,
                 max_iter=np.inf, verbose=False):
        # params
        self._seed = seed
        self._n_f = n_f
        self._lambda = lambda_
        self._conv = convergence_threshold
        self._max_iter = max_iter
        self._verbose = verbose
        self.u = None
        self.m = None

    def _update(self, r, x):
        """ Returns updated matrix M given U (if x=U), or matrix U given M
        otherwise

        Parameters
        ----------
        r : Dataset
            copy of R with movies as rows (if x=U), users as rows otherwise
        x : Dataset
            User or Movie feature matrix
        """
        results = []
        for subset in r:
            chunk_res = self._update_chunk(subset, x, n_f=self._n_f,
                                           lambda_=self._lambda)
            results.append(chunk_res)

        # matrix = results[0]
        # for i in range(1, len(results)):
        #     self.merge(matrix, results[i])

        # results = compss_wait_on(results)

        # print(matrix.shape)
        return self.merge(*results)

    @task(returns=object, isModifier=False)
    def merge(self, *chunks):
        if shallow_tracing:
            pro_f = sys.getprofile()
            sys.setprofile(None)
        res = np.vstack(chunks)

        if shallow_tracing:
            sys.setprofile(pro_f)

        return res
        # matrix = np.vstack([matrix, chunk])
        # print('matrix.shape: %s' % list(matrix.shape))

    @task(returns=np.array, isModifier=False)
    def _update_chunk(self, subset, x, n_f, lambda_):
        if shallow_tracing:
            pro_f = sys.getprofile()
            sys.setprofile(None)

        r_chunk = subset.samples
        n = r_chunk.shape[0]
        y = np.zeros((n, n_f), dtype=np.float32)
        n_c = np.array(
            [len(sparse.find(r_chunk[i])[0]) for i in
             range(0, r_chunk.shape[0])])
        for element in range(0, n):
            indices = sparse.find(r_chunk[element])[1]

            x_xt = x[indices].T.dot(x[indices])

            a_i = x_xt + lambda_ * n_c[element] * np.eye(n_f)
            v_i = x[indices].T.dot(r_chunk[element, indices].toarray().T)

            # for movielens and 200 factors times are:
            # y[element] = inv(a_i).dot(v_i).reshape(-1) # 20s
            # y[element] = np.linalg.solve(a_i, v_i).reshape(-1) # 13.87
            y[element] = sparse.linalg.cg(a_i, v_i)[0].reshape(-1)  # 4.06
        sys.setprofile(pro_f)
        print("y.size: %s" % list(y.shape))

        if shallow_tracing:
            sys.setprofile(pro_f)

        return y

    @task(returns=float, isModifier=False)
    def _get_rmse(self, test, u, m):
        if shallow_tracing:
            pro_f = sys.getprofile()
            sys.setprofile(None)

        pro_f = sys.getprofile()
        sys.setprofile(None)

        x_idxs, y_idxs, recs = sparse.find(test.samples)
        indices = zip(x_idxs, y_idxs)
        # import pdb
        # pdb.set_trace()
        preds = [u[x].dot(m[y].T) for x, y in indices]
        rmse = sqrt(mean_squared_error(recs, preds))

        if shallow_tracing:
            sys.setprofile(pro_f)
        return rmse

    def _has_converged(self, last_rmse, rmse, i):
        if i >= self._max_iter:
            if self._verbose:
                print("Max iterations reached [%s]" % self._max_iter)
            return True
        if i > 0 and abs(last_rmse - rmse) < self._conv:
            if self._verbose:
                print("Converged in %s iterations to difference < %s" % (
                    i, abs(last_rmse - rmse)))
            return True
        return False

    def fit(self, dataset, test=None):
        """ Returns updated matrix M given U (if x=U), or matrix U given M
        otherwise

        Parameters
        ----------
        dataset : Dataset
            Ratings matrix with movies as rows and users as columns.
        test : DataFrame
            Dataframe used to check convergence.
        """

        d_m = dataset
        d_u = d_m.transpose()

        n_m = d_u.n_features

        print("Movie chunks: %s" % len(d_m))
        print("User chunks: %s" % len(d_u))

        if self._seed:
            np.random.seed(self._seed)
        u = None
        m = np.random.rand(n_m, self._n_f)

        # Assign average rating as first feature
        average_ratings = d_m._apply(lambda row: np.mean(row.data),
                                     sparse=False, return_dataset=True)
        average_ratings = compss_wait_on(average_ratings)

        m[:, 0] = average_ratings.samples.reshape(-1)

        rmse, last_rmse = np.inf, np.NaN
        i = 0
        while not self._has_converged(last_rmse, rmse, i):
            start = time()
            last_rmse = rmse

            u = self._update(r=d_u, x=m)
            m = self._update(r=d_m, x=u)

            print("Update %s: %s" % (i, time() - start))

            if test is not None:
                x_idxs, y_idxs, recs = sparse.find(test)
                indices = zip(x_idxs, y_idxs)
                preds = [u[x].dot(m[y].T) for x, y in indices]
                rmse = sqrt(mean_squared_error(recs, preds))
                if self._verbose:
                    print("Test RMSE: %.3f  [%s]" % (
                    rmse, abs(last_rmse - rmse)))

            else:
                rmses = [self._get_rmse(sb, u, m) for sb in d_u._subsets]
                rmse = np.mean(compss_wait_on(rmses))
                if self._verbose:
                    print("Train RMSE: %.3f  [%s]" % (
                    rmse, abs(last_rmse - rmse)))
            print("Iter %s: %s" % (i, time() - start))
            i += 1

        self.u = compss_wait_on(u)
        self.m = compss_wait_on(m)

        return u, m

    def predict_user(self, user_id):
        if self.u is None or self.m is None:
            raise Exception("Model not trained, call first model.fit()")
        if user_id > self.u.shape[1]:
            return np.full([self.m.shape[1]], np.nan)

        return self.u[user_id].dot(self.m.T)