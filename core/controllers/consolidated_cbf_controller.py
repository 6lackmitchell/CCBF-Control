from this import d
import numpy as np
import builtins
from typing import Callable, List, overload
from importlib import import_module
from nptyping import NDArray
from control import lqr
from scipy.linalg import block_diag, null_space
from .cbfs.cbf import Cbf
from core.controllers.cbf_qp_controller import CbfQpController
from core.controllers.controller import Controller
from core.solve_cvxopt import solve_qp_cvxopt

vehicle = builtins.PROBLEM_CONFIG['vehicle']
control_level = builtins.PROBLEM_CONFIG['control_level']
system_model = builtins.PROBLEM_CONFIG['system_model']
mod = vehicle + '.' + control_level + '.models'

# Programmatic import
try:
    module = import_module(mod)
    globals().update({'f': getattr(module, 'f')})
    globals().update({'g': getattr(module, 'g')})
    globals().update({'sigma': getattr(module, 'sigma_{}'.format(system_model))})
except ModuleNotFoundError as e:
    print('No module named \'{}\' -- exiting.'.format(mod))
    raise e


class ConsolidatedCbfController(CbfQpController):
    """
    Class docstrings should contain the following information:

    Controller using the Consolidated CBF based approach first proposed 
    in 'Adaptation for Validation of a Consolidated Control Barrier Function
    based Control Synthesis' (Black and Panagou, 2022) 
    (https://arxiv.org/abs/2209.08170).

    Public Methods:
    ---------------
    formulate_qp: overloads from parent Controller class
    generate_consolidated_cbf_condition: generates matrix and vector for CBF condition
    compute_kdots: computes the adaptation of the gains k

    Class Properties:
    -----------------
    Lots?
    """

    P = None

    def __init__(self,
                 u_max: List,
                 nAgents: int,
                 objective_function: Callable,
                 nominal_controller: Controller,
                 cbfs_individual: List,
                 cbfs_pairwise: List,
                 ignore: List = None):
        super().__init__(u_max,
                         nAgents,
                         objective_function,
                         nominal_controller,
                         cbfs_individual,
                         cbfs_pairwise,
                         ignore)
        nCBF = len(self.cbf_vals)
        self.c_cbf = 100
        self.k_gains = 0.2 * np.ones((nCBF,))
        self.k_dots = np.zeros((nCBF,))
        self.n_agents = 1
        self.U_mat = self.u_max[:, np.newaxis] @ self.u_max[np.newaxis, :]

    def formulate_qp(self,
                     t: float,
                     ze: NDArray,
                     zr: NDArray,
                     u_nom: NDArray,
                     ego: int,
                     cascade: bool = False) -> (NDArray, NDArray, NDArray, NDArray, NDArray, NDArray, float):
        """Configures the Quadratic Program parameters (Q, p for objective function, A, b for inequality constraints,
        G, h for equality constraints).

        """
        # Parameters
        na = 1 + len(zr)
        ns = len(ze)
        self.safety = True
        discretization_error = 0.0

        if self.nv > 0:
            alpha_nom = 1.0
            Q, p = self.objective(np.append(u_nom.flatten(), alpha_nom))
            Au = block_diag(*(na + self.nv) * [self.au])[:-2, :-1]
            bu = np.append(np.array(na * [self.bu]).flatten(), self.nv * [1e6, 0])
        else:
            Q, p = self.objective(u_nom.flatten())
            Au = block_diag(*(na) * [self.au])
            bu = np.array(na * [self.bu]).flatten()

        # Initialize inequality constraints
        lci = len(self.cbfs_individual)
        h_array = np.zeros((len(self.cbf_vals,)))
        Lfh_array = np.zeros((len(self.cbf_vals,)))
        Lgh_array = np.zeros((len(self.cbf_vals), u_nom.flatten().shape[0]))

        # Iterate over individual CBF constraints
        for cc, cbf in enumerate(self.cbfs_individual):
            h0 = cbf.h0(ze)
            h_array[cc] = cbf.h(ze)
            dhdx = cbf.dhdx(ze)

            # Stochastic Term -- 0 for deterministic systems
            if np.trace(sigma(ze).T @ sigma(ze)) > 0 and self._stochastic:
                d2hdx2 = cbf.d2hdx2(ze)
                stoch = 0.5 * np.trace(sigma(ze).T @ d2hdx2 @ sigma(ze))
            else:
                stoch = 0.0

            # Get CBF Lie Derivatives
            Lfh_array[cc] = dhdx @ f(ze) + stoch - discretization_error
            Lgh_array[cc, self.nu * ego:(ego + 1) * self.nu] = dhdx @ g(ze)  # Only assign ego control
            if cascade:
                Lgh_array[cc, self.nu * ego] = 0.0

            self.cbf_vals[cc] = h_array[cc]
            if h0 < 0:
                self.safety = False

        # Iterate over pairwise CBF constraints
        for cc, cbf in enumerate(self.cbfs_pairwise):

            # Iterate over all other vehicles
            for ii, zo in enumerate(zr):
                other = ii + (ii >= ego)
                idx = lci + cc * zr.shape[0] + ii

                h0 = cbf.h0(ze, zo)
                h_array[idx] = cbf.h(ze, zo)
                dhdx = cbf.dhdx(ze,  zo)

                # Stochastic Term -- 0 for deterministic systems
                if np.trace(sigma(ze).T @ sigma(ze)) > 0 and self._stochastic:
                    d2hdx2 = cbf.d2hdx2(ze, zo)
                    stoch = 0.5 * (np.trace(sigma(ze).T @ d2hdx2[:ns, :ns] @ sigma(ze)) +
                                   np.trace(sigma(zo).T @ d2hdx2[ns:, ns:] @ sigma(zo)))
                else:
                    stoch = 0.0

                # Get CBF Lie Derivatives
                Lfh_array[idx] = dhdx[:ns] @ f(ze) + dhdx[ns:] @ f(zo) + stoch - discretization_error
                Lgh_array[idx, self.nu * ego:(ego + 1) * self.nu] = dhdx[:ns] @ g(ze)
                Lgh_array[idx, self.nu * other:(other + 1) * self.nu] = dhdx[ns:] @ g(zo)
                if cascade:
                    Lgh_array[idx, self.nu * ego] = 0.0

                if h0 < 0:
                    print("{} SAFETY VIOLATION: {:.2f}".format(str(self.__class__).split('.')[-1], -h0))
                    self.safety = False

                self.cbf_vals[idx] = h_array[idx]

        # Format inequality constraints
        Ai, bi = self.generate_consolidated_cbf_condition(ego, h_array, Lfh_array, Lgh_array)

        A = np.vstack([Au, Ai])
        b = np.hstack([bu, bi])

        return Q, p, A, b, None, None

    def generate_consolidated_cbf_condition(self,
                                            ego: int,
                                            h_array: NDArray,
                                            Lfh_array: NDArray,
                                            Lgh_array: NDArray) -> (NDArray, NDArray):
        """Generates the inequality constraint for the consolidated CBF.

        ARGUMENTS
            ego: ego vehicle id
            h: array of candidate CBFs
            Lfh: array of CBF drift terms
            Lgh: array of CBF control matrices

        RETURNS
            kdot: array of time-derivatives of k_gains
        """
        # Introduce discretization error
        discretization_error = 1.0

        # Get C-CBF Value
        exp_term = np.exp(-self.k_gains * h_array)
        H = 1 - np.sum(exp_term)  # Get value of C-CBF
        self.c_cbf = H

        # Non-centralized agents CBF dynamics become drifts
        Lgh_uncontrolled = np.copy(Lgh_array[:, :])
        Lgh_uncontrolled[:, ego * self.nu:(ego + 1) * self.nu] = 0
        Lgh_array[:, np.s_[:ego * self.nu]] = 0  # All indices before first ego index set to 0
        Lgh_array[:, np.s_[(ego + 1) * self.nu:]] = 0  # All indices after last ego index set to 0 (excluding alpha)

        # Get time-derivatives of gains
        premultiplier_k = self.k_gains * exp_term
        premultiplier_h = h_array * exp_term

        # Compute C-CBF Dynamics
        LfH = premultiplier_k @ Lfh_array 
        LgH = premultiplier_k @ Lgh_array
        LgH_uncontrolled = premultiplier_k @ Lgh_uncontrolled

        # Tunable CBF Addition
        # kH = 1.0
        # kH = 0.5
        kH = 0.05
        kH = 1.0
        phi = np.tile(-np.array(self.u_max), int(LgH_uncontrolled.shape[0] / len(self.u_max))) @ abs(LgH_uncontrolled) * np.exp(-kH * H)
        LfH = LfH + phi

        # Compute k dots
        Lg_for_kdot = Lgh_array[:, self.nu * ego:self.nu * (ego + 1)]
        k_dots = self.compute_k_dots(h_array, H, LfH, Lg_for_kdot, premultiplier_k)
        LfH = LfH + premultiplier_h @ k_dots - discretization_error

        if H < 0:
            print(f"h_array: {h_array}")
            print(f"LfH: {LfH}")
            print(f"LgH: {LgH}")
            print(f"H:   {H}")
            print(self.k_gains)
            print(H)

        # Finish constructing CBF here
        a_mat = np.append(-LgH, -H)
        b_vec = np.array([LfH])

        # Update k_dot
        self.k_gains = self.k_gains + k_dots * self._dt

        return a_mat[:, np.newaxis].T, b_vec

    def compute_k_dots(self,
                       h_array: NDArray,
                       H: float, 
                       LfH: float,
                       Lgh_array: NDArray,
                       vec: NDArray) -> NDArray:
        """Computes the time-derivatives of the k_gains via QP based adaptation law.

        ARGUMENTS
            h_array: array of CBF values
            H: scalar c-cbf value
            LfH: scalar LfH term for c-cbf
            Lgh_array: 2D array of Lgh terms
            vec: vector that needs to stay out of the nullspace of matrix P

        RETURNS
            k_dot

        """
        # Good behavior gains
        k_min = 0.1
        k_max = 1.0
        k_des = 10 * k_min * h_array
        k_des = np.clip(k_des, k_min, k_max)
        P_gain = 10.0
        constraint_gain = 10.0
        gradient_gain = 0.01

        # (3 Reached but H negative) gains
        k_min = 0.01
        k_max = 1.0
        k_des = h_array / 5
        k_des = np.clip(k_des, k_min, k_max)
        P_gain = 100.0
        constraint_gain = 1.0
        gradient_gain = 0.001

        # Testing gains
        k_min = 0.01
        k_max = 5.0
        k_des = 0.1 * h_array / np.min([np.min(h_array), 2.0])
        k_des = np.clip(k_des, k_min, k_max)
        P_gain = 1000.0
        kpositive_gain = 1000.0
        constraint_gain = 1000.0
        gradient_gain = 0.1

        # TO DO: Correct how delta is being used -- need the max over all safe set
        # Some parameters
        P_mat = P_gain * np.eye(len(h_array))
        p_vec = vec
        N_mat = null_space(Lgh_array.T)
        rank_Lg = N_mat.shape[0] - N_mat.shape[1]
        Q_mat = np.eye(len(self.k_gains)) - 2 * N_mat @ N_mat.T + N_mat @ N_mat.T @ N_mat @ N_mat.T
        _, sigma, _ = np.linalg.svd(Lgh_array.T)  # Singular values of LGH
        if rank_Lg > 0:
            sigma_r = sigma[rank_Lg - 1]  # minimum non-zero singular value
        else:
            sigma_r = 0  # This should not ever happen (by assumption that not all individual Lgh = 0)
        delta = -(LfH + 0.1 * H + (h_array * np.exp(-self.k_gains * h_array)) @ self.k_dots) / np.linalg.norm(self.u_max)
        delta = 1 * abs(delta)

        # Objective Function and Partial Derivatives
        J = 1 / 2 * (self.k_gains - k_des).T @ P_mat @ (self.k_gains - k_des)
        dJdk = P_mat @ (self.k_gains - k_des)
        d2Jdk2 = P_mat

        # K Constraint Functions and Partial Derivatives: Convention hi >= 0
        hi = (self.k_gains - k_min) * kpositive_gain
        dhidk = np.ones((len(self.k_gains),)) * kpositive_gain
        d2hidk2 = np.zeros((len(self.k_gains),len(self.k_gains))) * kpositive_gain

        # Partial Derivatives of p vector
        dpdk = np.diag((self.k_gains * h_array - 1) * np.exp(-self.k_gains * h_array))
        d2pdk2_vals = h_array * np.exp(-self.k_gains * h_array) + (self.k_gains * h_array - 1) * -h_array * np.exp(-self.k_gains * h_array)
        d2pdk2 = np.zeros((len(h_array), len(h_array), len(h_array)))
        np.fill_diagonal(d2pdk2, d2pdk2_vals)

        # # LGH Norm Constraint Function and Partial Derivatives
        # # Square of norm
        # eta = (sigma_r**2 * (p_vec.T @ Q_mat @ p_vec) - delta**2) * constraint_gain
        # detadk = 2 * sigma_r**2 * (dpdk.T @ Q_mat @ p_vec) * constraint_gain
        # d2etadk2 = (2 * sigma_r**2 * (d2pdk2.T @ Q_mat @ p_vec + dpdk.T @ Q_mat @ dpdk)) * constraint_gain

        # V vec
        v_vec = (p_vec @ Lgh_array).T
        dvdk = (dpdk @ Lgh_array).T
        d2vdk2 = (d2pdk2 @ Lgh_array).T

        # New Input Constraints Condition
        eta = ((v_vec.T @ U_mat @ v_vec) - delta**2) * constraint_gain
        detadk = 2 * (dvdk.T @ U_mat @ v_vec) * constraint_gain
        d2etadk2 = (2 * (d2vdk2.T @ U_mat @ v_vec + dvdk.T @ U_mat @ dvdk)) * constraint_gain

        print(f"Eta: {eta}")

        if np.isnan(eta):
            print(eta)

        # Define the augmented cost function
        Phi = J - np.sum(np.log(hi)) - np.log(eta)
        dPhidk = dJdk - np.sum(dhidk / hi) - detadk / eta
        d2Phidk2 = d2Jdk2 - np.sum(d2hidk2 / hi - dhidk / hi**2) - (d2etadk2 / eta - detadk / eta**2)

        # Define Adaptation law
        corrector_term = -np.linalg.inv(d2Phidk2) @ (dPhidk)  # Correction toward minimizer
        # corrector_term = -dPhidk  # Correction toward minimizer -- gradient method only
        predictor_term = 0 * self.k_gains  # Zero for now (need to see how this could depend on time)
        k_dots = gradient_gain * (corrector_term + predictor_term)

        if np.sum(np.isnan(k_dots)) > 0:
            print(k_dots)

        self.k_dots = k_dots

        return k_dots

    def grad_Phi_kx(self, x: NDArray, k: NDArray) -> NDArray:
        """Computes the gradient of Phi with respect to first
        the gains k and then the state x.

        Arguments
        ---------
        x: state vector
        k: constituent cbf weighting vector

        Returns
        -------
        grad_Phi_kx

        """
        grad_Phi_kx = (self.c0(x, k) * self.grad_c0_kx(x, k) - self.grad_c0_k(x, k) * self.grad_c0_x(x, k)) / self.c0(x, k)**2

        return grad_Phi_kx

    # TO DO: Fix how to handle delta
    def c0(self, x: NDArray, k: NDArray, h: NDArray, Lg: NDArray) -> float:
        """Returns the viability constraint function evaluated at the current
        state x and gain vector k.

        Arguments
        ---------
        x: state vector
        k: constituent cbf weighting vector
        h: constituent cbf vector
        Lg: matrix of constituent cbf Lgh vectors

        Returns
        -------
        c0: viability constraint function evaluated at x and k

        """
        # TO DO
        delta = 1

        q_vec = k * np.exp(-k * h)

        return q_vec @ Lg @ self.U_mat @ Lg.T @ q_vec.T - delta**2

    # TO DO: Fix how to handle delta
    def grad_c0_k(self, x: NDArray, k: NDArray, h: NDArray, Lg: NDArray) -> NDArray:
        """Computes the gradient of the viability constraint function evaluated at the current
        state x and gain vector k with respect to k.

        Arguments
        ---------
        x: state vector
        k: constituent cbf weighting vector
        h: constituent cbf vector
        Lg: matrix of constituent cbf Lgh vectors

        Returns
        -------
        grad_c0_k: gradient of viability constraint function with respect to k

        """
        # TO DO
        delta = 1  # Need to fix
        ddeltadk = np.ones((len(k),))  # Need to fix

        q_vec = k * np.exp(-k * h)
        dqdk = np.diag((k * h - 1) * np.exp(-k * h))

        grad_c0_k = 2 * dqdk @ Lg @ self.U_mat @ Lg.T @ q_vec.T - 2 * delta * ddeltadk

        return grad_c0_k

    # TO DO: Handle delta, handle dhdx for all h, handle dLgdx
    def grad_c0_x(self, x: NDArray, k: NDArray, h: NDArray, Lg: NDArray) -> NDArray:
        """Computes the gradient of the viability constraint function evaluated at the current
        state x and gain vector k with respect to x.

        Arguments
        ---------
        x: state vector
        k: constituent cbf weighting vector
        h: constituent cbf vector
        Lg: matrix of constituent cbf Lgh vectors

        Returns
        -------
        grad_c0_x: gradient of viability constraint function with respect to x

        """
        # TO DO
        dhdx = np.ones((len(k), len(x)))
        dLgdx = np.ones((len(k), 2, len(x)))
        delta = 1  # Need to fix
        ddeltadx = np.ones((len(x),))  # Need to fix

        q_vec = k * np.exp(-k * h)
        dqdx = -k**2 * np.exp(-k * h) @ dhdx

        grad_c0_x = 2 * dqdx @ Lg @ self.U_mat @ Lg.T @ q_vec.T + 2 * q_vec @ dLgdx @ self.U_mat @ Lg.T @ q_vec.T - 2 * delta * ddeltadx

        return grad_c0_x

    # TO DO: NEED TO FINISH
    def grad_c0_kx(self, x: NDArray, k: NDArray, h: NDArray, Lg: NDArray) -> NDArray:
        """Computes the gradient of the viability constraint function evaluated at the current
        state x and gain vector k with respect to first k and then x.

        Arguments
        ---------
        x: state vector
        k: constituent cbf weighting vector
        h: constituent cbf vector

        Returns
        -------
        grad_c0_kx: gradient of viability constraint function with respect to k then x

        """
        dhdx = np.ones((len(k), len(x)))
        dLgdx = np.ones((len(k), 2, len(x)))
        delta = 1  # Need to fix
        ddeltadk = np.ones((len(k),))  # Need to fix
        ddeltadx = np.ones((len(x),))  # Need to fix
        d2deltadxdk = np.ones((len(x), len(k)))  # Need to fix

        q_vec = k * np.exp(-k * h)
        dqdk = np.diag((k * h - 1) * np.exp(-k * h))
        dqdx = -k**2 * np.exp(-k * h) @ dhdx
        d2qdxdk = (k**2 * h - 2 * k) * np.exp(-k * h) @ dhdx

        grad_c0_kx = 2 * d2qdxdk @ Lg @ self.U_mat @ Lg.T @ q_vec.T + 2 * dqdx @ Lg @ self.U_mat @ Lg.T @ dqdk.T + 2 * dqdk @ dLgdx @ self.U_mat @ Lg.T @ q_vec.T + 2 * q_vec @ dLgdx @ self.U_mat @ Lg.T @ dqdk.T - 2 * (ddeltadk @ ddeltadx + delta * d2deltadxdk)

        return grad_c0_kx
        


    def k_dot_lqr(self,
                  h_array: NDArray) -> NDArray:
        """Computes the nominal k_dot adaptation law based on LQR.

        Arguments
            h_array: array of CBF values

        RETURNS
            k_dot_nominal: nominal k_dot adaptation law

        """
        gain = 50.0
        gain = 1.0
        # k_star = gain * h_array / np.max([np.min(h_array), 0.5])  # Desired k values
        k_star = np.ones((len(h_array),))

        # Integrator dynamics
        Ad = np.zeros((self.k_gains.shape[0], self.k_gains.shape[0]))
        Bd = np.eye(self.k_gains.shape[0])

        # Compute LQR gain
        Q = 0.01 * Bd
        R = 1 * Bd
        K, _, _ = lqr(Ad, Bd, Q, R)

        k_dot_lqr = -K @ (self.k_gains - k_star)
        k_dot_bounds = 5.0
        # k_dot_bounds = 100.0

        return np.clip(k_dot_lqr, -k_dot_bounds, k_dot_bounds)
