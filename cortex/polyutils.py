from collections import OrderedDict
import numpy as np
from scipy.spatial import distance, Delaunay, cKDTree
from scipy import sparse
import functools

def _memo(fn):
    """Helper decorator memoizes the given zero-argument function.
    """
    @functools.wraps(fn)
    def memofn(self):
        if id(fn) not in self._cache:
            self._cache[id(fn)] = fn(self)
        return self._cache[id(fn)]

    return memofn

class Surface(object):
    def __init__(self, pts, polys):
        self.pts = pts
        self.polys = polys

        self._cache = dict()
        self._rlfac_solvers = dict()
        self._nLC_solvers = dict()

    @property
    @_memo
    def ppts(self):
        """3D matrix of points in each face: n faces x 3 points per face x 3 coords per point.
        """
        return self.pts[self.polys]
    
    @property
    @_memo
    def connected(self):
        """Sparse matrix of vertex-face associations.
        """
        npt = len(self.pts)
        npoly = len(self.polys)
        return sparse.coo_matrix((np.ones((3*npoly,)), # data
                                  (np.hstack(self.polys.T), # row
                                   np.tile(range(npoly),(1,3)).squeeze())), # col
                                 (npt, npoly)).tocsr() # size

    @property
    @_memo
    def face_normals(self):
        """Normal vector for each face.
        """
        return np.cross(self.ppts[:,1] - self.ppts[:,0], self.ppts[:,2] - self.ppts[:,0])

    @property
    @_memo
    def vertex_normals(self):
        """Normal vector for each vertex (average of normals for neighboring faces).
        """
        return np.nan_to_num(self.connected.dot(self.face_normals) / self.connected.sum(1))

    @property
    @_memo
    def face_areas(self):
        """Area of each face.
        """
        return np.sqrt((self.face_normals**2).sum(-1))

    @property
    @_memo
    def cotangent_weights(self):
        """Cotangent of angle opposite each vertex in each face.
        """
        ppts = self.ppts
        cots1 = ((ppts[:,1]-ppts[:,0]) *
                 (ppts[:,2]-ppts[:,0])).sum(1) / np.sqrt((np.cross(ppts[:,1]-ppts[:,0],
                                                                   ppts[:,2]-ppts[:,0])**2).sum(1))
        cots2 = ((ppts[:,2]-ppts[:,1]) *
                 (ppts[:,0]-ppts[:,1])).sum(1) / np.sqrt((np.cross(ppts[:,2]-ppts[:,1],
                                                                   ppts[:,0]-ppts[:,1])**2).sum(1))
        cots3 = ((ppts[:,0]-ppts[:,2]) *
                 (ppts[:,1]-ppts[:,2])).sum(1) / np.sqrt((np.cross(ppts[:,0]-ppts[:,2],
                                                                   ppts[:,1]-ppts[:,2])**2).sum(1))

        return np.vstack([cots1, cots2, cots3])

    @property
    @_memo
    def laplace_operator(self):
        """Laplace-Beltrami operator for this surface. A sparse adjacency matrix with
        edge weights determined by the cotangents of the angles opposite each edge.
        Returns a triplet (D,W,V) where D is the 'lumped mass matrix', W is the weighted
        adjacency matrix, and V is a diagonal matrix that normalizes the adjacencies.
        The 'stiffness matrix', A, can be computed as V - W.
        
        See 'Discrete Laplace-Beltrami operators for shape analysis and segmentation'
        by Reuter et al., 2009 for details.
        """
        ## Lumped mass matrix
        D = self.connected.dot(self.face_areas) / 3.0

        ## Stiffness matrix
        npt = len(self.pts)
        cots1, cots2, cots3 = self.cotangent_weights
        # W is weighted adjacency matrix
        W1 = sparse.coo_matrix((cots1, (self.polys[:,1], self.polys[:,2])), (npt, npt))
        W2 = sparse.coo_matrix((cots2, (self.polys[:,2], self.polys[:,0])), (npt, npt))
        W3 = sparse.coo_matrix((cots3, (self.polys[:,0], self.polys[:,1])), (npt, npt))
        W = (W1 + W1.T + W2 + W2.T + W3 + W3.T).tocsr() / 2.0
        
        # V is sum of each col
        V = sparse.dia_matrix((np.array(W.sum(0)).ravel(),[0]), (npt,npt))

        # A is stiffness matrix
        #A = W - V # negative operator -- more useful in practice

        return D, W, V

    @property
    @_memo
    def avg_edge_length(self):
        """Average length of all edges in the surface.
        """
        adj = self.laplace_operator[1] # use laplace operator as adjacency matrix
        tadj = sparse.triu(adj, 1) # only entries above main diagonal, in coo format
        edgelens = np.sqrt(((self.pts[tadj.row] - self.pts[tadj.col])**2).sum(1))
        return edgelens.mean()

    def geodesic_distance(self, verts, m=1.0):
        """Minimum mesh geodesic distance (in mm) from each vertex in surface to any
        vertex in the collection verts.

        Geodesic distance is estimated using heat-based method (see 'Geodesics in Heat',
        Crane et al, 2012). The diffusion of heat along the mesh is simulated and then
        used to infer geodesic distance. The duration of the simulation is controlled
        by the parameter m. Larger values of m will smooth & regularize the distance
        computation. Smaller values of m will roughen and will usually increase error
        in the distance computation. The default value of 1.0 is probably pretty good.

        This function caches some data (sparse LU factorizations of the laplace-beltrami
        operator and the weighted adjacency matrix), so it will be much faster on
        subsequent runs.

        The time taken by this function is independent of the number of vertices in verts.
        """
        npt = len(self.pts)
        D, W, V = self.laplace_operator
        nLC = W - V # negative laplace matrix
        spD = sparse.dia_matrix((D,[0]), (npt,npt)).tocsr() # lumped mass matrix
        t = m * self.avg_edge_length ** 2 # time of heat evolution
        lfac = spD - t * nLC # backward Euler matrix

        # Exclude rows with zero weight (these break the sparse LU, that finicky fuck)
        goodrows = np.nonzero(~np.array(lfac.sum(0) == 0).ravel())[0]
        
        if m not in self._rlfac_solvers:
            self._rlfac_solvers[m] = sparse.linalg.dsolve.factorized(lfac[goodrows][:,goodrows])
        # Also solve system to get phi, the distances
        if m not in self._nLC_solvers:
            self._nLC_solvers[m] = sparse.linalg.dsolve.factorized(nLC[goodrows][:,goodrows])

        # Solve system to get u, the heat values
        u0 = np.zeros((npt,)) # initial heat values
        u0[verts] = 1.0
        goodu = self._rlfac_solvers[m](u0[goodrows])
        u = np.zeros((npt,))
        u[goodrows] = goodu

        # Compute grad u at each face
        ppts = self.ppts
        fnorms = self.face_normals
        pu = u[self.polys]
        e12 = ppts[:,1] - ppts[:,0]
        e23 = ppts[:,2] - ppts[:,1]
        e31 = ppts[:,0] - ppts[:,2]
        gradu = ((np.cross(fnorms, e12).T * pu[:,2] +
                  np.cross(fnorms, e23).T * pu[:,0] +
                  np.cross(fnorms, e31).T * pu[:,1]) / (2 * self.face_areas)).T

        # Compute X (normalized grad u)
        X = (-gradu.T / np.sqrt((gradu**2).sum(1))).T

        # Compute integrated divergence of X at each vertex
        cots1, cots2, cots3 = self.cotangent_weights
        divx = np.zeros((npt,))
        x1 = 0.5 * (cots3 * ((ppts[:,1]-ppts[:,0]) * X).sum(1) +
                    cots2 * ((ppts[:,2]-ppts[:,0]) * X).sum(1))
        x2 = 0.5 * (cots1 * ((ppts[:,2]-ppts[:,1]) * X).sum(1) +
                    cots3 * ((ppts[:,0]-ppts[:,1]) * X).sum(1))
        x3 = 0.5 * (cots2 * ((ppts[:,0]-ppts[:,2]) * X).sum(1) +
                    cots1 * ((ppts[:,1]-ppts[:,2]) * X).sum(1))

        divx = (sparse.coo_matrix((x1, (self.polys[:,0]*0, self.polys[:,0])), (1,npt)) +
                sparse.coo_matrix((x2, (self.polys[:,0]*0, self.polys[:,1])), (1,npt)) +
                sparse.coo_matrix((x3, (self.polys[:,0]*0, self.polys[:,2])), (1,npt))).todense().A.squeeze()

        # Compute phi (distance)
        goodphi = self._nLC_solvers[m](divx[goodrows])
        phi = np.zeros((npt,))
        phi[goodrows] = goodphi - goodphi.min()

        return phi

    def extract_chunk(self, nfaces=100, seed=None, auxpts=None):
        '''Extract a chunk of the surface using breadth first search, for testing purposes'''
        node = seed
        if seed is None:
            node = np.random.randint(len(self.pts))

        ptmap = dict()
        queue = [node]
        faces = set()
        visited = set([node])
        while len(faces) < nfaces and len(queue) > 0:
            node = queue.pop(0)
            for face in self.connected[node].indices:
                if face not in faces:
                    faces.add(face)
                    for pt in self.polys[face]:
                        if pt not in visited:
                            visited.add(pt)
                            queue.append(pt)

        pts, aux, polys = [], [], []
        for face in faces:
            for pt in self.polys[face]:
                if pt not in ptmap:
                    ptmap[pt] = len(pts)
                    pts.append(self.pts[pt])
                    if auxpts is not None:
                        aux.append(auxpts[pt])
            polys.append([ptmap[p] for p in self.polys[face]])

        if auxpts is not None:
            return np.array(pts), np.array(aux), np.array(polys)

        return np.array(pts), np.array(polys)

    def smooth(self, scalars, neighborhood=2, smooth=8):
        if len(scalars) != len(self.pts):
            raise ValueError('Each point must have a single value')
            
        def getpts(pt, n):
            for face in self.connected[pt].indices:
                for pt in self.polys[face]:
                    if n == 0:
                        yield pt
                    else:
                        for q in getpts(pt, n-1):
                            yield q
        
        from . import mp
        def func(i):
            val = scalars[i]
            neighbors = list(set(getpts(i, neighborhood)))
            if len(neighbors) > 0:
                g = np.exp(-((scalars[neighbors] - val)**2) / (2*smooth**2))
                return (g * scalars[neighbors]).mean()
            else:
                return 0

        return np.array(mp.map(func, range(len(scalars))))

    def polyhedra(self, wm):
        '''Iterates through the polyhedra that make up the closest volume to a certain vertex'''
        for p, facerow in enumerate(self.connected):
            faces = facerow.indices
            pts, polys = _ptset(), _quadset()
            if len(faces) > 0:
                poly = np.roll(self.polys[faces[0]], -np.nonzero(self.polys[faces[0]] == p)[0][0])
                assert pts[wm[p]] == 0
                assert pts[self.pts[p]] == 1
                pts[wm[poly[[0, 1]]].mean(0)]
                pts[self.pts[poly[[0, 1]]].mean(0)]

                for face in faces:
                    poly = np.roll(self.polys[face], -np.nonzero(self.polys[face] == p)[0][0])
                    a = pts[wm[poly].mean(0)]
                    b = pts[self.pts[poly].mean(0)]
                    c = pts[wm[poly[[0, 2]]].mean(0)]
                    d = pts[self.pts[poly[[0, 2]]].mean(0)]
                    e = pts[wm[poly[[0, 1]]].mean(0)]
                    f = pts[self.pts[poly[[0, 1]]].mean(0)]

                    polys((0, c, a, e))
                    polys((1, f, b, d))
                    polys((1, d, c, 0))
                    polys((1, 0, e, f))
                    polys((f, e, a, b))
                    polys((d, b, a, c))

            yield pts.points, np.array(list(polys.triangles))

    #def neighbors(self, n=1):
    #    def neighbors(pt, n):
    #        current = set(self.polys[self.connected[pt].indices])
    #        if n - 1 > 0:
    #            next = set()
    #            for pt in current:
    #                next = next | neighbors(pt, n-1)
    #
    #        return current | next

    def patches(self, auxpts=None, n=1):
        def align_polys(p, polys):
            x, y = np.nonzero(polys == p)
            y = np.vstack([y, (y+1)%3, (y+2)%3]).T
            return polys[np.tile(x, [3, 1]).T, y]

        def half_edge_align(p, pts, polys):
            poly = align_polys(p, polys)
            mid   = pts[poly].mean(1)
            left  = pts[poly[:,[0,2]]].mean(1)
            right = pts[poly[:,[0,1]]].mean(1)
            s1 = np.array(np.broadcast_arrays(pts[p], mid, left)).swapaxes(0,1)
            s2 = np.array(np.broadcast_arrays(pts[p], mid, right)).swapaxes(0,1)
            return np.vstack([s1, s2])

        def half_edge(p, pts, polys):
            poly = align_polys(p, polys)
            mid   = pts[poly].mean(1)
            left  = pts[poly[:,[0,2]]].mean(1)
            right = pts[poly[:,[0,1]]].mean(1)
            stack = np.vstack([mid, left, right, pts[p]])
            return stack[(distance.cdist(stack, stack) == 0).sum(0) == 1]

        for p, facerow in enumerate(self.connected):
            faces = facerow.indices
            if len(faces) > 0:
                if n == 1:
                    if auxpts is not None:
                        pidx = np.unique(self.polys[faces])
                        yield np.vstack([self.pts[pidx], auxpts[pidx]])
                    else:
                        yield self.pts[self.polys[faces]]
                elif n == 0.5:
                    if auxpts is not None:
                        pts = half_edge(p, self.pts, self.polys[faces])
                        aux = half_edge(p, auxpts, self.polys[faces])
                        yield np.vstack([pts, aux])
                    else:
                        yield half_edge_align(p, self.pts, self.polys[faces])
                else:
                    raise ValueError
            else:
                yield None

    def edge_collapse(self, p1, p2, target):
        raise NotImplementedError
        face1 = self.connected[p1]
        face2 = self.connected[p2]

class _ptset(object):
    def __init__(self):
        self.idx = OrderedDict()
    def __getitem__(self, idx):
        idx = tuple(idx)
        if idx not in self.idx:
            self.idx[idx] = len(self.idx)
        return self.idx[idx]

    @property
    def points(self):
        return np.array(list(self.idx.keys()))

class _quadset(object):
    def __init__(self):
        self.polys = dict()

    def __call__(self, quad):
        idx = tuple(sorted(quad))
        if idx in self.polys:
            del self.polys[idx]
        else:
            self.polys[idx] = quad

    @property
    def triangles(self):
        for quad in list(self.polys.values()):
            yield quad[:3]
            yield [quad[0], quad[2], quad[3]]

class Distortion(object):
    def __init__(self, flat, ref, polys):
        self.flat = flat
        self.ref = ref
        self.polys = polys

    @property
    def areal(self):
        def area(pts, polys):
            ppts = pts[polys]
            cross = np.cross(ppts[:,1] - ppts[:,0], ppts[:,2] - ppts[:,0])
            return np.sqrt((cross**2).sum(-1))

        refarea = area(self.ref, self.polys)
        flatarea = area(self.flat, self.polys)
        tridists = np.log2(flatarea/refarea)
        
        vertratios = np.zeros((len(self.ref),))
        vertratios[self.polys[:,0]] += tridists
        vertratios[self.polys[:,1]] += tridists
        vertratios[self.polys[:,2]] += tridists
        vertratios /= np.bincount(self.polys.ravel())
        vertratios = np.nan_to_num(vertratios)
        vertratios[vertratios==0] = 1
        return vertratios

    @property
    def metric(self):
        import networkx as nx
        def iter_surfedges(tris):
            for a,b,c in tris:
                yield a,b
                yield b,c
                yield a,c

        def make_surface_graph(tris):
            graph = nx.Graph()
            graph.add_edges_from(iter_surfedges(tris))
            return graph

        G = make_surface_graph(self.polys)
        selverts = np.unique(self.polys.ravel())
        ref_dists = [np.sqrt(((self.ref[G.neighbors(ii)] - self.ref[ii])**2).sum(1))
                     for ii in selverts]
        flat_dists = [np.sqrt(((self.flat[G.neighbors(ii)] - self.flat[ii])**2).sum(1))
                      for ii in selverts]
        msdists = np.array([(fl-fi).mean() for fi,fl in zip(ref_dists, flat_dists)])
        alldists = np.zeros((len(self.ref),))
        alldists[selverts] = msdists
        return alldists

def tetra_vol(pts):
    '''Volume of a tetrahedron'''
    tetra = pts[1:] - pts[0]
    return np.abs(np.dot(tetra[0], np.cross(tetra[1], tetra[2]))) / 6

def brick_vol(pts):
    '''Volume of a triangular prism'''
    return tetra_vol(pts[[0, 1, 2, 4]]) + tetra_vol(pts[[0, 2, 3, 4]]) + tetra_vol(pts[[2, 3, 4, 5]])

def sort_polys(polys):
    amin = polys.argmin(1)
    xind = np.arange(len(polys))
    return np.array([polys[xind, amin], polys[xind, (amin+1)%3], polys[xind, (amin+2)%3]]).T

def face_area(pts):
    '''Area of triangles

    Parameters
    ----------
    pts : array_like
        n x 3 x 3 array with n triangles, 3 pts, and (x,y,z) coordinates
    '''
    return 0.5 * np.sqrt((np.cross(pts[:,1]-pts[:,0], pts[:,2]-pts[:,0])**2).sum(1))

def face_volume(pts1, pts2, polys):
    '''Volume of each face in a polyhedron sheet'''
    vols = np.zeros((len(polys),))
    for i, face in enumerate(polys):
        vols[i] = brick_vol(np.append(pts1[face], pts2[face], axis=0))
        if i % 1000 == 0:
            print(i)
    return vols

def decimate(pts, polys):
    from tvtk.api import tvtk
    pd = tvtk.PolyData(points=pts, polys=polys)
    dec = tvtk.DecimatePro(input=pd)
    dec.set(preserve_topology=True, splitting=False, boundary_vertex_deletion=False, target_reduction=1.0)
    dec.update()
    dpts = dec.output.points.to_array()
    dpolys = dec.output.polys.to_array().reshape(-1, 4)[:,1:]
    return dpts, dpolys

def curvature(pts, polys):
    '''Computes mean curvature using VTK'''
    from tvtk.api import tvtk
    pd = tvtk.PolyData(points=pts, polys=polys)
    curv = tvtk.Curvatures(input=pd, curvature_type="mean")
    curv.update()
    return curv.output.point_data.scalars.to_array()

def inside_convex_poly(pts):
    """Returns a function that checks if inputs are inside the convex hull of polyhedron defined by pts

    Alternative method to check is to get faces of the convex hull, then check if each normal is pointed away from each point.
    As it turns out, this is vastly slower than using qhull's find_simplex, even though the simplex is not needed.
    """
    tri = Delaunay(pts)
    return lambda x: tri.find_simplex(x) != -1

def make_cube(center=(.5, .5, .5), size=1):
    pts = np.array([(0, 0, 0), (1, 0, 0), (0, 1, 0), (1, 1, 0),
                    (0, 0, 1), (1, 0, 1), (0, 1, 1), (1, 1, 1)], dtype=float)
    pts -= (.5, .5, .5)
    polys = np.array([(0, 2, 3), (0, 3, 1), (0, 1, 4), (1, 5, 4),
                      (1, 3, 5), (3, 7, 5), (2, 7, 3), (2, 6, 7),
                      (0, 6, 2), (0, 4, 6), (4, 7, 6), (4, 5, 7)], dtype=np.uint32)
    return pts * size + center, polys

def boundary_edges(polys):
    '''Returns the edges that are on the boundary of a mesh, as defined by belonging to only 1 face'''
    edges = dict()
    for i, poly in enumerate(np.sort(polys)):
        for a, b in [(0,1), (1,2), (0, 2)]:
            key = poly[a], poly[b]
            if key not in edges:
                edges[key] = []
            edges[key].append(i)

    epts = []
    for edge, faces in edges.items():
        if len(faces) == 1:
            epts.append(edge)

    return np.array(epts)

def trace_poly(edges):
    '''Given a disjoint set of edges, yield complete linked polygons'''
    idx = dict((i, set([])) for i in np.unique(edges))
    for i, (x, y) in enumerate(edges):
        idx[x].add(i)
        idx[y].add(i)

    eset = set(range(len(edges)))
    while len(eset) > 0:
        eidx = eset.pop()
        poly = list(edges[eidx])
        stack = set([eidx])
        while poly[-1] != poly[0] or len(poly) == 1:
            next = list(idx[poly[-1]] - stack)[0]
            eset.remove(next)
            stack.add(next)
            if edges[next][0] == poly[-1]:
                poly.append(edges[next][1])
            elif edges[next][1] == poly[-1]:
                poly.append(edges[next][0])
            else:
                raise Exception

        yield poly

def rasterize(poly, shape=(256, 256)):
    #ImageDraw sucks at its job, so we'll use imagemagick to do rasterization
    import subprocess as sp
    import cStringIO
    import shlex
    import Image
    
    polygon = " ".join(["%0.3f,%0.3f"%tuple(p[::-1]) for p in np.array(poly)-(.5, .5)])
    cmd = 'convert -size %dx%d xc:black -fill white -stroke none -draw "polygon %s" PNG32:-'%(shape[0], shape[1], polygon)
    proc = sp.Popen(shlex.split(cmd), stdout=sp.PIPE)
    png = cStringIO.StringIO(proc.communicate()[0])
    im = Image.open(png)

    # For PNG8:
    # mode, palette = im.palette.getdata()
    # lut = np.fromstring(palette, dtype=np.uint8).reshape(-1, 3)
    # if (lut == 255).any():
    #     white = np.nonzero((lut == 255).all(1))[0][0]
    #     return np.array(im) == white
    # return np.zeros(shape, dtype=bool)
    return (np.array(im)[:,:,0] > 128).T

def voxelize(pts, polys, shape=(256, 256, 256), center=(128, 128, 128), mp=True):
    from tvtk.api import tvtk
    import Image
    import ImageDraw
    
    pd = tvtk.PolyData(points=pts + center + (0, 0, 0), polys=polys)
    plane = tvtk.Planes(normals=[(0,0,1)], points=[(0,0,0)])
    clip = tvtk.ClipPolyData(clip_function=plane, input=pd)
    feats = tvtk.FeatureEdges(
        manifold_edges=False, 
        non_manifold_edges=False, 
        feature_edges=False,
        boundary_edges=True,
        input=clip.output)

    def func(i):
        plane.points = [(0,0,i)]
        feats.update()
        vox = np.zeros(shape[:2][::-1], np.uint8)
        if feats.output.number_of_lines > 0:
            epts = feats.output.points.to_array()
            edges = feats.output.lines.to_array().reshape(-1, 3)[:,1:]
            for poly in trace_poly(edges):
                vox += rasterize(epts[poly][:,:2]+[.5, .5], shape=shape[:2][::-1])
        return vox % 2

    if mp:
        from . import mp
        layers = mp.map(func, range(shape[2]))
    else:
        layers = map(func, range(shape[2]))

    return np.array(layers).T
