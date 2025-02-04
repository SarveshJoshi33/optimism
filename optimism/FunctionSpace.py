from jax.scipy.linalg import solve
from jaxtyping import Array, Float
from optimism import Interpolants
from optimism import Mesh
from optimism import QuadratureRule
from typing import Tuple, List
import equinox as eqx
import jax
import jax.numpy as np
import numpy as onp


class EssentialBC(eqx.Module):
    nodeSet: str
    component: int


class FunctionSpace(eqx.Module):
    """Data needed for calculus on functions in the discrete function space.

    In describing the shape of the attributes, ``ne`` is the number of
    elements in the mesh, ``nqpe`` is the number of quadrature points per
    element, ``npe`` is the number of nodes per element, and ``nd`` is the
    spatial dimension of the domain.

    Attributes:
        shapes: Shape function values on each element, shape (ne, nqpe, npe)
        vols: Volume attributed to each quadrature point. That is, the
            quadrature weight (on the parameteric element domain) multiplied by
            the Jacobian determinant of the map from the parent element to the
            element in the domain. Shape (ne, nqpe).
        shapeGrads: Derivatives of the shape functions with respect to the
            spatial coordinates of the domain. Shape (ne, nqpe, npe, nd).
        mesh: The ``Mesh`` object of the domain.
        quadratureRule: The ``QuadratureRule`` on which to sample the shape
            functions.
        isAxisymmetric: boolean indicating if the function space data are
            axisymmetric.
    """
    shapes: Float[Array, "ne nqpe nn"]
    vols: Float[Array, "ne nqpe"]
    shapeGrads: Float[Array, "ne nqpe nn nd"]
    mesh: Mesh.Mesh
    quadratureRule: QuadratureRule.QuadratureRule
    isAxisymmetric: bool


def construct_function_space(mesh, quadratureRule, mode2D='cartesian'):
    """Construct a discrete function space.

    Parameters
    ----------
    mesh: The mesh of the domain.
    quadratureRule: The quadrature rule to be used for integrating on the
        domain.
    mode2D: A string indicating how the 2D domain is interpreted for
        integration. Valid values are ``cartesian`` and ``axisymmetric``.
        Axisymetric mode will include the factor of 2*pi*r in the ``vols``
        attribute.

    Returns
    -------
    The ``FunctionSpace`` object.
    """

    shapeOnRef = Interpolants.compute_shapes(mesh.parentElement, quadratureRule.xigauss)
    return construct_function_space_from_parent_element(mesh, shapeOnRef, quadratureRule, mode2D)


def construct_function_space_from_parent_element(mesh, shapeOnRef, quadratureRule, mode2D='cartesian'):
    """Construct a function space with precomputed shape function data on the parent element.

    This version of the function space constructor is Jax-transformable,
    and in particular can be jitted. The computation of the shape function
    values and derivatives on the parent element is not transformable in
    general. However, the mapping of the shape function data to the elements in
    the mesh is transformable. One can precompute the parent element shape
    functions once and for all, and then use this special factory function to
    construct the function space and avoid the non-transformable part of the
    operation. The primary use case is for shape sensitivities: the coordinates
    of the mesh change, and we want Jax to pick up the sensitivities of the
    shape function derivatives in space to the coordinate changes
    (which occurs through the mapping from the parent element to the spatial
    domain).

    Parameters
    ----------
    mesh: The mesh of the domain.
    shapeOnRef: A tuple of the shape function values and gradients on the
        parent element, evaluated at the quadrature points. The caller must
        take care to ensure the shape functions are evaluated at the same
        points as contained in the ``quadratureRule`` parameter.
    quadratureRule: The quadrature rule to be used for integrating on the
        domain.
    mode2D: A string indicating how the 2D domain is interpreted for
        integration. See the default factory function for details.

    Returns
    -------
    The ``FunctionSpace`` object.
    """

    shapes = jax.vmap(lambda elConns, elShape: elShape, (0, None))(mesh.conns, shapeOnRef.values)

    shapeGrads = jax.vmap(map_element_shape_grads, (None, 0, None, None))(mesh.coords, mesh.conns, mesh.parentElement, shapeOnRef.gradients)

    if mode2D == 'cartesian':
        el_vols = compute_element_volumes
        isAxisymmetric = False
    elif mode2D == 'axisymmetric':
        el_vols = compute_element_volumes_axisymmetric
        isAxisymmetric = True
    vols = jax.vmap(el_vols, (None, 0, None, 0, None))(mesh.coords, mesh.conns, mesh.parentElement, shapes, quadratureRule.wgauss)

    return FunctionSpace(shapes, vols, shapeGrads, mesh, quadratureRule, isAxisymmetric)


def map_element_shape_grads(coordField, nodeOrdinals, parentElement, shapeGradients):
    Xn = coordField.take(nodeOrdinals,0)
    v = Xn[parentElement.vertexNodes]
    J = np.column_stack((v[0] - v[2], v[1] - v[2]))
    return jax.vmap(lambda dN: solve(J.T, dN.T).T)(shapeGradients)


def compute_element_volumes(coordField, nodeOrdinals, parentElement, shapes, weights):
    Xn = coordField.take(nodeOrdinals,0)
    v = Xn[parentElement.vertexNodes]
    jac = np.cross(v[1] - v[0], v[2] - v[0])
    return jac*weights


def compute_element_volumes_axisymmetric(coordField, nodeOrdinals, parentElement, shapes, weights):
    vols = compute_element_volumes(coordField, nodeOrdinals, parentElement, shapes, weights)
    Xn = coordField.take(nodeOrdinals,0)
    Rs = shapes@Xn[:,0]
    return 2*np.pi*Rs*vols


# This kind of function space is now deprecated (09/26/22). Its main purpose was to be
# jax transformable for use in shape optimization. The ``construct_function_space_from_parent_element``
# factory function should now be used instead.
#
# only supports cartesian and linear shape functions on tris, single integration point per element
def construct_weighted_function_space(mesh, quadratureRule, quadratureWeights=1.0):
    
    def normal_vector(edge):
        ba = edge[1]-edge[0]
        return np.array([ba[1], -ba[0]])
    
    def compute_volume(coords):
        vol = 0.
        for e in range(3):
            edge = [coords[e], coords[(e+1)%3]]
            normal = normal_vector(edge)
            vol += normal[0] * 0.5*(edge[0][0]+edge[1][0])
        return vol

    def compute_elem_volume(coordField, elem):
        return compute_volume(coordField.take(elem,0))

    def compute_elem_linear_shape_gradient(coordField, vol, elem):
        coords = coordField.take(elem,0)
        shape = np.zeros([3,2])
        halfVol = 0.5/vol
        for e in range(3):
            n1 = e
            n2 = (e+1)%3
            edge = [coords[n1], coords[n2]]
            normal = normal_vector(edge)
            shape = shape.at[np.array([n1,n2])].add(normal*halfVol)
        return shape.reshape((1,3,2))
    
    shapes = np.ones( (mesh.conns.shape[0], 1, 3) )

    vols = jax.vmap(compute_elem_volume, (None, 0))(mesh.coords, mesh.conns)
    shapeGrads = jax.vmap(compute_elem_linear_shape_gradient, (None, 0, 0))(mesh.coords, vols, mesh.conns)
    
    vols = np.reshape( vols, (vols.shape[0], 1) )
    vols = vols * quadratureWeights
        
    return FunctionSpace(shapes, vols, shapeGrads, mesh, quadratureRule, isAxisymmetric=False)


def default_modify_element_gradient(elemGrads, elemShapes, elemVols, elemNodalDisps, elemNodalCoords):
    return elemGrads


def compute_field_gradient(functionSpace, nodalField, modify_element_gradient=default_modify_element_gradient):
    return jax.vmap(compute_element_field_gradient, (None,None,0,0,0,0,None))(nodalField, functionSpace.mesh.coords, functionSpace.shapes, functionSpace.shapeGrads, functionSpace.vols, functionSpace.mesh.conns, modify_element_gradient)


def interpolate_to_points(functionSpace, nodalField):
    return jax.vmap(interpolate_to_element_points, (None, 0, 0))(nodalField, functionSpace.shapes, functionSpace.mesh.conns)


def integrate_over_block(functionSpace, U, stateVars, dt, func, block,
                         *params, modify_element_gradient=default_modify_element_gradient):
    """Integrates a density function over a block of the mesh.

    Args:
      functionSpace: Function space object to do the integration with.
      U: The vector of dofs for the primal field in the functional.
      stateVars: Internal state variable array.
      dt: Current time increment
      func: Lagrangian density function to integrate, Must have the signature
        ``func(u, dudx, q, x, *params) -> scalar``, where ``u`` is the primal field, ``q`` is the
        value of the internal variables, ``x`` is the current point coordinates, and ``*params`` is
        a variadic set of additional parameters, which correspond to the ``*params`` argument.
      block: Group of elements to integrate over. This is an array of element indices. For
        performance, the elements within the block should be numbered consecutively.
      *params: Optional parameter fields to pass into Lagrangian density function. These are
        represented as a single value per element.
      modify_element_gradient: Optional function that modifies the gradient at the element level.
        This can be to set the particular 2D mode, and additionally to enforce volume averaging
        on the gradient operator. This is a keyword-only argument.

    Returns:
      A scalar value for the integral of the density functional ``func`` integrated over the
      block of elements.
    """
    
    vals = evaluate_on_block(functionSpace, U, stateVars, dt, func, block, *params, modify_element_gradient=modify_element_gradient)
    return np.dot(vals.ravel(), functionSpace.vols[block].ravel())


def evaluate_on_block(functionSpace, U, stateVars, dt, func, block,
                      *params, modify_element_gradient=default_modify_element_gradient):
    """Evaluates a density function at every quadrature point in a block of the mesh.

    Args:
      functionSpace: Function space object to do the evaluation with.
      U: The vector of dofs for the primal field in the functional.
      stateVars: Internal state variable array.
      dt: Current time increment
      func: Lagrangian density function to evaluate, Must have the signature
        ``func(u, dudx, q, x, *params) -> scalar``, where ``u`` is the primal field, ``q`` is the
        value of the internal variables, ``x`` is the current point coordinates, and ``*params`` is
        a variadic set of additional parameters, which correspond to the ``*params`` argument.
      block: Group of elements to evaluate over. This is an array of element indices. For
        performance, the elements within the block should be numbered consecutively.
      *params: Optional parameter fields to pass into Lagrangian density function. These are
        represented as a single value per element.
      modify_element_gradient: Optional function that modifies the gradient at the element level.
        This can be to set the particular 2D mode, and additionally to enforce volume averaging
        on the gradient operator. This is a keyword-only argument.

    Returns:
      An array of shape (numElements, numQuadPtsPerElement) that contains the scalar values of the
      density functional ``func`` at every quadrature point in the block.
    """
    fs = functionSpace
    compute_elem_values = jax.vmap(evaluate_on_element, (None, None, 0, None, 0, 0, 0, 0, None, None, *tuple(0 for p in params)))
    
    blockValues = compute_elem_values(U, fs.mesh.coords, stateVars[block], dt, fs.shapes[block],
                                      fs.shapeGrads[block], fs.vols[block],
                                      fs.mesh.conns[block], func, modify_element_gradient, *params)
    return blockValues


def integrate_element_from_local_field(elemNodalField, elemNodalCoords, elemStates, dt, elemShapes, elemShapeGrads, elemVols, func, modify_element_gradient=default_modify_element_gradient):
    """Integrate over element with element nodal field as input.
    This allows element residuals and element stiffness matrices to computed.
    """
    elemVals = jax.vmap(interpolate_to_point, (None,0))(elemNodalField, elemShapes)
    elemGrads = jax.vmap(compute_quadrature_point_field_gradient, (None,0))(elemNodalField, elemShapeGrads)
    elemGrads = modify_element_gradient(elemGrads, elemShapes, elemVols, elemNodalField, elemNodalCoords)
    elemPoints = jax.vmap(interpolate_to_point, (None,0))(elemNodalCoords, elemShapes)
    fVals = jax.vmap(func, (0, 0, 0, 0, None))(elemVals, elemGrads, elemStates, elemPoints, dt)
    return np.dot(fVals, elemVols)


def compute_element_field_gradient(U, coords, elemShapes, elemShapeGrads, elemVols, elemConnectivity, modify_element_gradient):
    elemNodalDisps = U[elemConnectivity]
    elemGrads = jax.vmap(compute_quadrature_point_field_gradient, (None, 0))(elemNodalDisps, elemShapeGrads)
    elemNodalCoords = coords[elemConnectivity]
    elemGrads = modify_element_gradient(elemGrads, elemShapes, elemVols, elemNodalDisps, elemNodalCoords)
    return elemGrads


def compute_quadrature_point_field_gradient(u, shapeGrad):
    dg = np.tensordot(u, shapeGrad, axes=[0,0])
    return dg


def interpolate_to_point(elementNodalValues, shape):
    return np.dot(shape, elementNodalValues)


def interpolate_to_element_points(U, elemShapes, elemConnectivity):
    elemU = U[elemConnectivity]
    return jax.vmap(interpolate_to_point, (None, 0))(elemU, elemShapes)


def integrate_element(U, coords, elemStates, elemShapes, elemShapeGrads, elemVols, elemConn, func, modify_element_gradient):
    elemVals = interpolate_to_element_points(U, elemShapes, elemConn)
    elemGrads = compute_element_field_gradient(U, coords, elemShapes, elemShapeGrads, elemVols, elemConn, modify_element_gradient)
    elemXs = interpolate_to_element_points(coords, elemShapes, elemConn)
    fVals = jax.vmap(func)(elemVals, elemGrads, elemStates, elemXs)
    return np.dot(fVals, elemVols)


def evaluate_on_element(U, coords, elemStates, dt, elemShapes, elemShapeGrads, elemVols, elemConn, kernelFunc, modify_element_gradient, *params):
    elemVals = interpolate_to_element_points(U, elemShapes, elemConn)
    elemGrads = compute_element_field_gradient(U, coords, elemShapes, elemShapeGrads, elemVols, elemConn, modify_element_gradient)
    elemXs = interpolate_to_element_points(coords, elemShapes, elemConn)
    vmapArgs = 0, 0, 0, 0, None, *tuple(None for p in params)
    fVals = jax.vmap(kernelFunc, vmapArgs)(elemVals, elemGrads, elemStates, elemXs, dt, *params)
    return fVals


def project_quadrature_field_to_element_field(functionSpace, quadField):
    return jax.vmap(average_quadrature_field_over_element)(quadField, functionSpace.vols)


def average_quadrature_field_over_element(elemQPData, vols):
    S = np.tensordot(vols, elemQPData, axes=[0,0])
    elVol = np.sum(vols)
    return S/elVol


def get_nodal_values_on_edge(functionSpace, nodalField, edge):
    """Get nodal values of a field on an element edge.

    Arguments:
    functionSpace: a FunctionSpace object
    nodalField: The nodal vector defined over the mesh (shape is number of
        nodes by number of field components)
    edge: tuple containing the element number containing the edge and the
        permutation (0, 1, or 2) of the edge within the triangle
    """
    edgeNodes = functionSpace.mesh.parentElement.faceNodes[edge[1], :]
    nodes = functionSpace.mesh.conns[edge[0], edgeNodes]
    return nodalField[nodes]


def interpolate_nodal_field_on_edge(functionSpace, U, interpolationPoints, edge):
    """Interpolate a nodal field to specified points on an element edge.

    Arguments:
    functionSpace: a FunctionSpace object
    U: the nodal values array
    interpolationPoints: coordinates of points (in the 1D parametric space) to
        interpolate to
    edge: tuple containing the element number containing the edge and the
        permutation (0, 1, or 2) of the edge within the triangle
    """
    edgeShapes = Interpolants.compute_shapes(functionSpace.mesh.parentElement1d, interpolationPoints)
    edgeU = get_nodal_values_on_edge(functionSpace, U, edge)
    return edgeShapes.values.T@edgeU


def integrate_function_on_edge(functionSpace, func, U, quadRule, edge):
    uq = interpolate_nodal_field_on_edge(functionSpace, U, quadRule.xigauss, edge)
    Xq = interpolate_nodal_field_on_edge(functionSpace, functionSpace.mesh.coords, quadRule.xigauss, edge)
    edgeCoords = Mesh.get_edge_coords(functionSpace.mesh, edge)
    _, normal, jac = Mesh.compute_edge_vectors(functionSpace.mesh, edgeCoords)
    integrand = jax.vmap(func, (0, 0, None))(uq, Xq, normal)
    return np.dot(integrand, jac*quadRule.wgauss)


def integrate_function_on_edges(functionSpace, func, U, quadRule, edges):
    integrate_on_edges = jax.vmap(integrate_function_on_edge, (None, None, None, None, 0))
    return np.sum(integrate_on_edges(functionSpace, func, U, quadRule, edges))

def create_nodeset_layers(mesh):
    coords = mesh.coords
    tol = 1e-8
    # Create unique layers of nodesets along the y-direction
    unique_layers = sorted(onp.unique(coords[:,1]))
    Ny = int(input("Enter the expected number of nodeset layers in y-direction: "))
    assert len(unique_layers) == Ny, f"ERROR - Expected {Ny} layers, but found {len(unique_layers)}."

    layer_rows = []

    for i, y_val in enumerate(unique_layers):
        nodes_in_layer = onp.flatnonzero(onp.abs(coords[:, 1] - y_val) < tol)
        layer_rows.append(nodes_in_layer)

    max_nodes_per_layer = max(len(row) for row in layer_rows)
    y_layers = -1 * np.ones((len(layer_rows), max_nodes_per_layer), dtype=int)  # Initialize with -1

    for i, row in enumerate(layer_rows):
        y_layers = y_layers.at[i, :len(row)].set(row)  # Fill each row with nodes from the layer

    # # For debugging
    # print("Layers in y-direction: ", y_layers)
    return y_layers


class DofManager(eqx.Module):
    # TODO get type hints below correct
    # TODO this one could be moved to jax types if we move towards
    # TODO jit safe preconditioners/solvers
    fieldShape: Tuple[int, int]
    isBc: any
    isUnknown: any
    ids: any
    unknownIndices: any
    bcIndices: any
    dofToUnknown: any
    HessRowCoords: any
    HessColCoords: any
    hessian_bc_mask: any

    def __init__(self, functionSpace, dim, EssentialBCs):
        self.fieldShape = Mesh.num_nodes(functionSpace.mesh), dim
        isBc = onp.full(self.fieldShape, False, dtype=bool)
        for ebc in EssentialBCs:
            isBc[functionSpace.mesh.nodeSets[ebc.nodeSet], ebc.component] = True
        self.isBc = isBc
        self.isUnknown = ~self.isBc

        self.ids = onp.arange(self.isBc.size).reshape(self.fieldShape)

        self.unknownIndices = self.ids[self.isUnknown]
        self.bcIndices = self.ids[self.isBc]

        ones = onp.ones(self.isBc.size, dtype=int) * -1
        dofToUnknown = ones
        dofToUnknown[self.unknownIndices] = onp.arange(self.unknownIndices.size)
        self.dofToUnknown = dofToUnknown

        self.HessRowCoords, self.HessColCoords = self._make_hessian_coordinates(functionSpace.mesh.conns)

        self.hessian_bc_mask = self._make_hessian_bc_mask(onp.array(functionSpace.mesh.conns))


    def get_bc_size(self):
        return np.sum(self.isBc).item() # item() method casts to Python int


    def get_unknown_size(self):
        return np.sum(self.isUnknown).item() # item() method casts to Python int


    def create_field(self, Uu, Ubc=0.0):
        U = np.zeros(self.isBc.shape).at[self.isBc].set(Ubc)
        return U.at[self.isUnknown].set(Uu)


    def get_bc_values(self, U):
        return U[self.isBc]


    def get_unknown_values(self, U):
        return U[self.isUnknown]


    def slice_unknowns_with_dof_indices(self, Uu, dofIndexSlice):
        i = self.isUnknown[dofIndexSlice]
        j = self.dofToUnknown.reshape(self.fieldShape)[dofIndexSlice]
        return Uu[j[i]]


    def _make_hessian_coordinates(self, conns):
        nElUnknowns = onp.zeros(conns.shape[0], dtype=int)
        nHessianEntries = 0
        for e, eNodes in enumerate(conns):
            elUnknownFlags = self.isUnknown[eNodes,:].ravel()
            nElUnknowns[e] = onp.sum(elUnknownFlags)
            nHessianEntries += onp.square(nElUnknowns[e])

        rowCoords = onp.zeros(nHessianEntries, dtype=int)
        colCoords = rowCoords.copy()
        rangeBegin = 0
        for e,eNodes in enumerate(conns):
            elDofs = self.ids[eNodes,:]
            elUnknownFlags = self.isUnknown[eNodes,:]
            elUnknowns = self.dofToUnknown[elDofs[elUnknownFlags]]
            elHessCoords = onp.tile(elUnknowns, (nElUnknowns[e],1))

            rangeEnd = rangeBegin + onp.square(nElUnknowns[e])

            rowCoords[rangeBegin:rangeEnd] = elHessCoords.ravel()
            colCoords[rangeBegin:rangeEnd] = elHessCoords.T.ravel()

            rangeBegin += onp.square(nElUnknowns[e])
        return rowCoords, colCoords


    def _make_hessian_bc_mask(self, conns):
        nElements, nNodesPerElement = conns.shape
        nFields = self.ids.shape[1]
        nDofPerElement = nNodesPerElement*nFields

        hessian_bc_mask = onp.full((nElements,nDofPerElement,nDofPerElement),
                                   True, dtype=bool)
        for e, eNodes in enumerate(conns):
            eFlag = self.isBc[eNodes,:].ravel()
            hessian_bc_mask[e,eFlag,:] = False
            hessian_bc_mask[e,:,eFlag] = False
        return hessian_bc_mask


# DofManager for Multi-Point Constrained Problem

class DofManagerMPC(eqx.Module):
    fieldShape: Tuple[int, int]
    isBc: np.ndarray
    isUnknown: np.ndarray
    ids: np.ndarray
    unknownIndices: np.ndarray
    bcIndices: np.ndarray
    dofToUnknown: np.ndarray
    HessRowCoords: np.ndarray
    HessColCoords: np.ndarray
    hessian_bc_mask: np.ndarray
    master_layer: int = eqx.static_field()
    slave_layer: int = eqx.static_field()
    master_array: np.ndarray
    slave_array: np.ndarray
    layers: List[List[int]] = eqx.static_field()

    def __init__(self, functionSpace, dim, EssentialBCs, mesh):
        self.fieldShape = (Mesh.num_nodes(functionSpace.mesh), dim)
        self.isBc = onp.full(self.fieldShape, False, dtype=bool)
        for ebc in EssentialBCs:
            self.isBc[functionSpace.mesh.nodeSets[ebc.nodeSet], ebc.component] = True
        self.isUnknown = ~self.isBc

        self.ids = onp.arange(self.isBc.size).reshape(self.fieldShape)

        # Create layers and assign master and slave rows
        self.layers = create_nodeset_layers(mesh)
        print("These are the layers created for the mesh: ", self.layers)
        self.master_layer = int(input("Choose the row index for master nodes: "))
        self.slave_layer = int(input("Choose the row index for slave nodes: "))

        # Generate master and slave arrays
        self.master_array = self._create_layer_array(self.master_layer)
        self.slave_array = self._create_layer_array(self.slave_layer)

        # Create DOF mappings for unknowns
        self.unknownIndices = self.ids[self.isUnknown]
        self.bcIndices = self.ids[self.isBc]

        ones = np.ones(self.isBc.size, dtype=int) * -1
        self.dofToUnknown = ones.at[self.unknownIndices].set(np.arange(self.unknownIndices.size))

        # Compute Hessian-related data
        self.HessRowCoords, self.HessColCoords = self._make_hessian_coordinates(functionSpace.mesh.conns)
        self.hessian_bc_mask = self._make_hessian_bc_mask(functionSpace.mesh.conns)

    def get_bc_size(self):
        return np.sum(self.isBc).item()

    def get_unknown_size(self):
        return np.sum(self.isUnknown).item()

    def create_field(self, Uu, Ubc=0.0):
        U = np.zeros(self.isBc.shape).at[self.isBc].set(Ubc)
        return U.at[self.isUnknown].set(Uu)

    def get_bc_values(self, U):
        return U[self.isBc]

    def get_unknown_values(self, U):
        return U[self.isUnknown]

    def get_master_values(self, U):
        return U[self.master_array[:, 1:]]

    def get_slave_values(self, U):
        return U[self.slave_array[:, 1:]]

    def _create_layer_array(self, layer_index):
        """Creates an array for the specified layer (master or slave) with DOF mappings."""
        layer_nodes = self.layers[layer_index]
        layer_array = []
        for node in layer_nodes:
            node_dofs = self.ids[node]
            layer_array.append([node, *node_dofs])
        return np.array(layer_array, dtype=int)

    def _make_hessian_coordinates(self, conns):
        """Creates row and column coordinates for the Hessian, considering master and slave nodes."""
        nElUnknowns = onp.zeros(conns.shape[0], dtype=int)
        nHessianEntries = 0
        for e, eNodes in enumerate(conns):
            elUnknownFlags = self.isUnknown[eNodes, :].ravel()
            nElUnknowns[e] = onp.sum(elUnknownFlags)

            # Include master and slave nodes in the size calculation
            eNodes_list = onp.asarray(eNodes).tolist()
            elMasterNodes = set(eNodes_list).intersection(set(onp.array(self.master_array[:, 0])))
            elSlaveNodes = set(eNodes_list).intersection(set(onp.array(self.slave_array[:, 0])))

            nElMasters = sum(len(self.master_array[onp.where(self.master_array[:, 0] == node)[0], 1:].ravel()) for node in elMasterNodes)
            nElSlaves = sum(len(self.slave_array[onp.where(self.slave_array[:, 0] == node)[0], 1:].ravel()) for node in elSlaveNodes)

            totalDOFs = nElUnknowns[e] + nElMasters + nElSlaves
            nHessianEntries += totalDOFs ** 2

        # Allocate sufficient space for Hessian coordinates
        rowCoords = onp.zeros(nHessianEntries, dtype=int)
        colCoords = onp.zeros(nHessianEntries, dtype=int)
        rangeBegin = 0

        for e, eNodes in enumerate(conns):
            eNodes_list = onp.asarray(eNodes).tolist()
            elDofs = self.ids[eNodes, :]
            elUnknownFlags = self.isUnknown[eNodes, :]
            elUnknowns = self.dofToUnknown[elDofs[elUnknownFlags]]

            # Identify master and slave DOFs for the element
            elMasterNodes = set(eNodes_list).intersection(set(onp.array(self.master_array[:, 0])))
            elSlaveNodes = set(eNodes_list).intersection(set(onp.array(self.slave_array[:, 0])))

            elMasterDofs = []
            for node in elMasterNodes:
                master_idx = onp.where(self.master_array[:, 0] == node)[0]
                elMasterDofs.extend(self.master_array[master_idx, 1:].ravel())

            elSlaveDofs = []
            for node in elSlaveNodes:
                slave_idx = onp.where(self.slave_array[:, 0] == node)[0]
                elSlaveDofs.extend(self.slave_array[slave_idx, 1:].ravel())

            # Combine unknowns, master, and slave DOFs
            elAllUnknowns = onp.concatenate([elUnknowns, elMasterDofs, elSlaveDofs])
            elHessCoords = onp.tile(elAllUnknowns, (len(elAllUnknowns), 1))

            nElHessianEntries = len(elAllUnknowns) ** 2
            rangeEnd = rangeBegin + nElHessianEntries

            # Assign values to the Hessian coordinate arrays
            rowCoords[rangeBegin:rangeEnd] = elHessCoords.ravel()
            colCoords[rangeBegin:rangeEnd] = elHessCoords.T.ravel()

            rangeBegin += nElHessianEntries

        return rowCoords, colCoords

    def _make_hessian_bc_mask(self, conns):
        """Creates a mask for BCs in the Hessian, considering master and slave nodes."""
        nElements, nNodesPerElement = conns.shape
        nFields = self.ids.shape[1]
        nDofPerElement = nNodesPerElement * nFields

        hessian_bc_mask = onp.full((nElements, nDofPerElement, nDofPerElement), True, dtype=bool)
        for e, eNodes in enumerate(conns):
            # Get boundary condition flags for all DOFs
            eFlag = self.isBc[eNodes, :].ravel()

            # Identify master and slave nodes
            eNodes_list = onp.asarray(eNodes).tolist()
            masterFlag = onp.isin(eNodes_list, self.master_array[:, 0])
            slaveFlag = onp.isin(eNodes_list, self.slave_array[:, 0])

            # Expand master and slave flags to match the DOF shape
            masterFlag_expanded = onp.repeat(masterFlag, nFields)
            slaveFlag_expanded = onp.repeat(slaveFlag, nFields)

            # Combine flags
            combinedFlag = eFlag | masterFlag_expanded | slaveFlag_expanded

            # Update Hessian mask
            hessian_bc_mask[e, combinedFlag, :] = False
            hessian_bc_mask[e, :, combinedFlag] = False

        return hessian_bc_mask



