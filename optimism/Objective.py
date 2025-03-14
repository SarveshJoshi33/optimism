from optimism.JaxConfig import *
from optimism.SparseCholesky import SparseCholesky
import numpy as onp
import jax.numpy as np
import jax
from jax import jit, grad, jacfwd, jvp, vjp
from scipy.sparse import csc_matrix
from scipy.sparse import diags as sparse_diags 


# static vs dynamics
# differentiable vs undifferentiable
Params = namedtuple('Params',
                    ['bc_data',
                     'state_data',
                     'design_data',
                     'app_data',
                     'time',
                     'dynamic_data'],
                     defaults=(None,None,None,None,None,None))


def param_index_update(p, index, newParam):
    if index==0:
        return Params(newParam, p[1], p[2], p[3], p[4], p[5])
    if index==1:
        return Params(p[0], newParam, p[2], p[3], p[4], p[5])
    if index==2:
        return Params(p[0], p[1], newParam, p[3], p[4], p[5])
    if index==3:
        return Params(p[0], p[1], p[2], newParam, p[4], p[5])
    if index==4:
        return Params(p[0], p[1], p[2], p[3], newParam, p[5])
    if index==5:
        return Params(p[0], p[1], p[2], p[3], p[4], newParam)
    print('invalid index passed to param_index_update = ', index)


class PrecondStrategy:

    def __init__(self, objective_precond):
        self.objective_precond = objective_precond
        
        
    def initialize(self, x, p):
        self.K = self.objective_precond(x, p)
    
    
    def precond_at_attempt(self, attempt):
        if attempt==0:
            return self.K
        else:
            dAbs = onp.abs(self.K.diagonal())
            shift = pow(10, (-5+attempt))
            return self.K + sparse_diags( shift * dAbs, 0, format='csc' )


class TwoTryPrecondStrategy(PrecondStrategy):

    def __init__(self, f1, f2):
        self.f1 = f1
        self.f2 = f2

        
    def initialize(self, x, p):
        self.x = x
        self.p = p

        
    def precond_at_attempt(self, attempt):
        if attempt==0:
            return self.f1(self.x, self.p)
        elif attempt==1:
            self.K = self.f2(self.x, self.p)
            return self.K
        else:
            dAbs = onp.abs(self.K.diagonal())
            shift = pow(10, (-5+attempt))
            return self.K + sparse_diags(shift * dAbs, format='csc')
        
    
class Objective:

    def __init__(self, f, x, p, precondStrategy=None):

        self.precond = SparseCholesky()
        self.precondStrategy = precondStrategy
        
        self.p = p
        
        self.objective=jit(f)
        self.grad_x = jit(grad(f,0))
        self.grad_p = jit(grad(f,1))
        
        self.hess_vec   = jit(lambda x, p, vx:
                              jvp(lambda z: self.grad_x(z,p), (x,), (vx,))[1])

        self.vec_hess   = jit(lambda x, p, vx:
                              vjp(lambda z: self.grad_x(z,p), x)[1](vx))
        
        self.jac_xp_vec = jit(lambda x, p, vp0:
                              jvp(lambda q0: self.grad_x(x, param_index_update(p,0,q0)),
                                  (p[0],),
                                  (vp0,))[1])

        self.jac_xp2_vec = jit(lambda x, p, vp2:
                               jvp(lambda q2: self.grad_x(x, param_index_update(p,2,q2)),
                                   (p[2],),
                                   (vp2,))[1])
        
        self.vec_jac_xp0 = jit(lambda x, p, vx:
                               vjp(lambda q0: self.grad_x(x, param_index_update(p,0,q0)), p[0])[1](vx))
        
        self.vec_jac_xp1 = jit(lambda x, p, vx:
                               vjp(lambda q1: self.grad_x(x, param_index_update(p,1,q1)), p[1])[1](vx))
        
        self.vec_jac_xp2 = jit(lambda x, p, vx:
                               vjp(lambda q2: self.grad_x(x, param_index_update(p,2,q2)), p[2])[1](vx))

        self.vec_jac_xp4 = jit(lambda x, p, vx:
                               vjp(lambda q4: self.grad_x(x, param_index_update(p,4,q4)), p[4])[1](vx))


        self.grad_and_tangent = lambda x, p: linearize(lambda z: self.grad_x(z,p), x)
        
        self.hess = jit(jacfwd(self.grad_x, 0))

        self.scaling = 1.0
        self.invScaling = 1.0
        
        
    def value(self, x):
        return self.objective(x, self.p)

    def gradient(self, x):
        return self.grad_x(x, self.p)
    
    def gradient_p(self, x):
        return self.grad_p(x, self.p)

    def hessian_vec(self, x, vx):
        return self.hess_vec(x, self.p, vx)

    def gradient_and_tangent(self, x):
        return self.grad_and_tangent(x, self.p)
    
    def vec_hessian(self, x, vx):
        return self.vec_hess(x, self.p, vx)
                              
    def hessian(self, x):
        return self.hess(x, self.p)

    def jacobian_p_vec(self, x, vp):
        return self.jac_xp_vec(x, self.p, vp)

    def jacobian_p2_vec(self, x, vp):
        return self.jac_xp2_vec(x, self.p, vp)

    def vec_jacobian_p0(self, x, vp):
        return self.vec_jac_xp0(x, self.p, vp)
    
    def vec_jacobian_p1(self, x, vp):
        return self.vec_jac_xp1(x, self.p, vp)

    def vec_jacobian_p2(self, x, vp):
        return self.vec_jac_xp2(x, self.p, vp)

    def vec_jacobian_p4(self, x, vp):
        return self.vec_jac_xp4(x, self.p, vp)
        
    def apply_precond(self, vx):
        if self.precond:
            return self.precond.apply(vx)
        else:
            return vx

    def multiply_by_approx_hessian(self, vx):
        if self.precond:
            return self.precond.multiply_by_approximate(vx)
        else:
            return vx

    def update_precond(self, x):
        if self.precondStrategy==None:
            print('Updating with dense preconditioner in Objective.')
            K = csc_matrix(self.hessian(x))
            def stiffness_at_attempt(attempt):
                if attempt==0:
                    return K
                else:
                    dAbs = onp.abs(K.diagonal())
                    shift = pow(10, (-5+attempt))
                    return K + sparse_diags(shift * dAbs, 0, format='csc')
            self.precond.update(stiffness_at_attempt)
        else:
            self.precondStrategy.initialize(x, self.p)
            self.precond.update(self.precondStrategy.precond_at_attempt)

    def check_stability(self, x):
        if self.precond:
            self.precond.check_stability(x, self.p)


class ScaledPrecondStrategy(PrecondStrategy):

    def __init__(self,
                 precondStrategy,
                 dofScaling):
        self.ps = precondStrategy
        self.invScaling = sparse_diags(onp.array(dofScaling), format='csc')

    
    def initialize(self, x, p):        
        self.ps.initialize(self.invScaling*x, p)

        
    def precond_at_attempt(self, attempt):
        K = self.ps.precond_at_attempt(attempt)
        K2 = csc_matrix( self.invScaling.T * K * self.invScaling )
        
        Kdiag = np.array(K2.diagonal())

        print('min, max diagonal stiffness = ',
              np.min(Kdiag),
              np.max(Kdiag))
        return K2


class ScaledObjective(Objective):
    
    def __init__(self,
                 objective_func,
                 x0,
                 p,
                 precondStrategy=None):
        
        if precondStrategy:
            precondStrategy.initialize(x0, p)
            K0 = precondStrategy.precond_at_attempt(0)
            scaling = np.sqrt(K0.diagonal())
            invScaling = 1.0/scaling
            
            scaledPrecondStrategy = ScaledPrecondStrategy(precondStrategy,
                                                          invScaling)

        else:
            scaling = 1.0
            invScaling = 1.0
            scaledPrecondStrategy = None
            
        def scaled_objective(xBar, p):
            x = invScaling * xBar
            return objective_func(x, p)

        xBar0 = scaling * x0
        super().__init__(scaled_objective,
                         xBar0,
                         p,
                         scaledPrecondStrategy)

        self.scaling = scaling
        self.invScaling = invScaling
        

    def get_value(self, x):
        return self.value(self.scaling * x)
    

    def get_residual(self, x):
        return self.gradient(self.scaling * x)

# Objective class for handling Multi-Point Constraints

# class ObjectiveMPC:
#     def __init__(self, f, x, p, dofManagerMPC, precondStrategy=None):
#         """
#         Objective function for Multi-Point Constraints (MPC), ensuring 
#         condensation of independent DOFs (ui, up).
#         """
#         self.precond = None
#         self.precondStrategy = precondStrategy
#         self.dofManagerMPC = dofManagerMPC  # Store DOF Manager with MPC
#         self.p = p

#         # JIT compile function derivatives
#         self.objective = jit(f)
#         self.grad_x = jit(grad(f, 0))
#         self.grad_p = jit(grad(f, 1))
#         self.hess = jit(jacfwd(self.grad_x, 0))

#         # Create transformation matrix & shift vector
#         self.T = self.dofManagerMPC.T
#         self.s_tilde = self.dofManagerMPC.s_tilde

#     def value(self, x):
#         """Compute objective function value."""
#         x_full = self.expand_to_full_dofs(x)
#         return self.objective(x_full, self.p)

#     def gradient(self, x):
#         """Compute reduced gradient."""
#         x_full = self.expand_to_full_dofs(x)
#         grad_full = self.grad_x(x_full, self.p)
#         return np.matmul(self.T.T, grad_full)  # Reduce gradient

#     def hessian(self, x):
#         """Compute reduced Hessian H̃ = Tᵀ H_full T"""
#         print("Computing Hessian with MPC...")
        
#         x_full = self.expand_to_full_dofs(x)
#         H_full = self.hess(x_full, self.p)  # Compute full Hessian

#         return np.matmul(self.T.T, np.matmul(H_full, self.T))  # Condensed Hessian

#     def expand_to_full_dofs(self, x):
#         """
#         Expand reduced DOF vector `x` (ui, up) to full DOFs (including uc)
#         using transformation: u = T ũ + s̃.
#         """
#         x_full = np.matmul(self.T, x) + self.s_tilde
#         return x_full

#     def update_precond(self, x):
#         """Update preconditioner with reduced Hessian."""
#         if self.precondStrategy is None:
#             print("Updating with dense preconditioner in ObjectiveMPC.")
#             H_reduced = csc_matrix(self.hessian(x))
#             self.precond = H_reduced
#         else:
#             self.precondStrategy.initialize(x, self.p)
#             self.precond = self.precondStrategy.precond_at_attempt

class ObjectiveMPC(Objective):
    def __init__(self, objective_func, dofManagerMPC, x0, p, precondStrategy=None):
        self.dofManagerMPC = dofManagerMPC
        self.T = np.array(dofManagerMPC.T)  # Transformation matrix
        self.s_tilde = np.array(dofManagerMPC.s_tilde)  # Shift vector
        self.scaling = 1.0  # Optional scaling factor
        self.invScaling = 1.0

        def condensed_objective(xBar, p):
            x = self.expand_to_full_dofs(xBar)
            return objective_func(x, p)
        
        xBar0 = self.reduce_to_independent_dofs(x0)
        super().__init__(condensed_objective, xBar0, p, precondStrategy)
    
    def reduce_to_independent_dofs(self, x_full):
        """Extracts independent DOFs (ui, up) from full DOF vector."""
        return np.matmul(self.T.T, x_full - self.s_tilde)

    def expand_to_full_dofs(self, x_reduced):
        """Expands reduced DOF vector (ui, up) to full DOFs including uc."""
        return np.matmul(self.T, x_reduced) + self.s_tilde
    
    def get_value(self, x):
        return self.value(self.expand_to_full_dofs(x))
    
    def get_residual(self, x):
        return self.gradient(self.expand_to_full_dofs(x))
    
    def hessian(self, x):
        """Computes the condensed Hessian for the independent DOFs."""
        x_full = self.expand_to_full_dofs(self.scaling * x)
        H_full = self.hess(x_full, self.p)  # Full Hessian from original problem
        H_reduced = np.matmul(self.T.T, np.matmul(H_full, self.T))  # Condensed Hessian
        return H_reduced
    
    def update_precond(self, x):
        """Updates preconditioner using the condensed Hessian."""
        print("Updating with condensed Hessian preconditioner.")
        H_reduced = csc_matrix(self.hessian(x))  # Use reduced Hessian
        self.precondStrategy.update(H_reduced)
