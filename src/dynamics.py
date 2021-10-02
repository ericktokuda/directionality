#!/usr/bin/env python3
"""Run experiments for the directed graphs survey
"""

import argparse
import time, datetime
import os, random
from os.path import join as pjoin
import inspect
from types import SimpleNamespace

import sys
import shutil
import numpy as np
import matplotlib; matplotlib.use('Agg')
import matplotlib.pyplot as plt
import igraph
from scipy.stats import pearsonr
from myutils import info, create_readme
import json
import scipy
import scipy.sparse as spa
from multiprocessing import Pool
import pandas as pd


##########################################################
SUSCEPTIBLE = 0
INFECTED = 1

##########################################################
def simu_intandfire(gin, threshold, tmax, trimsz):
    """Simplified integrate-and-fire.
    Adapted from @chcomin. If you use this code, please cite:
    'Structure and dynamics: the transition from nonequilibrium to equilibrium in
    integrate-and-fire dynamics', Comin et al., 2012
    """

    g = gin.copy()
    n = g.vcount()
    initial_charges = np.random.randint(int(threshold*1.3), size=g.vcount())

    edges = np.array(g.get_edgelist())
    par, child = zip(*edges)
    h = np.ones(edges.shape[0])
    A = spa.csr_matrix((h, (child, par)),shape=(n, n))

    acc = initial_charges.copy()

    for i in range(trimsz):
        is_spiking = (acc >= threshold)
        charge_gain = A.dot(is_spiking)
        acc = acc - acc*is_spiking + charge_gain

    fires = np.zeros(n, dtype=int)
    for i in range(tmax-trimsz):
        is_spiking = (acc >= threshold)
        fires += is_spiking
        charge_gain = A.dot(is_spiking)
        acc = acc - acc*is_spiking + charge_gain

    return fires, np.sum(is_spiking)

##########################################################
def infection_step(adj, status0, beta, gamma):
    """Individual iteration of the SIS transmission/recovery"""

    # Recovery
    status1 = status0.copy()
    randvals = np.random.rand(np.sum(status0))
    lucky = randvals < gamma
    infinds = np.where(status0)[0]
    recinds = infinds[np.where(lucky)]
    status1[recinds] = 0

    # Infection
    status2 = status1.copy()
    q = np.ones(len(adj), dtype=float) - beta # Prob of not being infected, q
    aux = adj[status1.astype(bool), :] # Filter out arcs departing from recovered
    kins = np.sum(aux, axis=0)
    probs = 1 - np.power(q, kins) # Prob of infecting is (1-q^kin)
    posprobids = np.where(probs)[0]
    posprobs = probs[posprobids]
    randvals = np.random.rand(len(posprobs))
    relinds = np.where(randvals < posprobs)
    status2[posprobids[relinds]] = 1
    balance = np.sum(status2) - np.sum(status0)

    return status2, status2 - status1

##########################################################
def set_initial_status(n, i0):
    """Set initial status"""
    status = np.zeros(n, dtype=int)
    choice = np.random.choice(range(n), size=i0, replace=False)
    status[choice] = INFECTED
    return status

##########################################################
def simu_sis(gin, beta, gamma, i0, trimsz, tmax):
    """Simulate the SIS epidemics model
    """
    adj = np.array(gin.get_adjacency().data)
    n = gin.vcount()

    status = set_initial_status(n, i0)

    for i in range(trimsz):
        status, _ = infection_step(adj, status, beta, gamma)

    ninfec = np.zeros(n, dtype=int)
    for i in range(tmax-trimsz):
        status, newinf = infection_step(adj, status, beta, gamma)
        ninfec += newinf

    return ninfec, np.sum(newinf)

##########################################################
def find_closest_factors(n):
    m = int(np.sqrt(n))
    while n % m != 0:
        m -= 1
    return m, n / m

##########################################################
def generate_data(top, n, k):
    """Generate data"""
    info(inspect.stack()[0][3] + '()')
    m = round(k / 2)

    h, w = find_closest_factors(n)

    if top == 'la':
        g = igraph.Graph.Lattice([w, h], nei=1, circular=False)
    elif top == 'er':
        erdosprob = k / n
        g = igraph.Graph.Erdos_Renyi(n, erdosprob)
    elif top == 'ba':
        g = igraph.Graph.Barabasi(n, m)
    elif top == 'ws':
        rewprob = 0.2
        g = igraph.Graph.Lattice([w, h], nei=1, circular=False)
        g.rewire_edges(rewprob)
    elif top == 'gr':
        ngr, r = get_rgg_params(n, k)
        mindiff = 999
        for i in range(3): # Get the graph with closest nvertices
            gnew = igraph.Graph.GRG(ngr, r).clusters().giant()
            if np.abs(gnew.vcount() - n) >= mindiff: continue
            g = gnew
            mindiff = np.abs(g.vcount() - n)
    elif top == 'sb':
        if k == 5: x = 4.5
        elif k == 6: x = 8.3
        elif k == 7: x = 12.5
        elif k == 8: x = 16.2
        pref = (np.array([[14, 1], [1, x]]) / n).tolist()
        n2 = n // 2
        szs = [ n2, n - n2 ]
        g = igraph.Graph.SBM(n, pref, szs, directed=False, loops=False)
    return g

##########################################################
def randomwalk(l, startnode, trans):
    """Random walk assuming a transition matrix with elements such that
    trans[i, j] represents the probability of i going j."""
    n = trans.shape[1]
    walk = - np.ones(l+1, dtype=int)
    walk[0] = startnode
    for i in range(l):
        walk[i+1] = np.random.choice(range(n), p=trans[walk[i], :])
    return walk

##########################################################
def remove_arc_conn(g):
    """Remove and arc while keep the graph strongly connected"""
    edgeids = np.arange(0, g.ecount())
    np.random.shuffle(edgeids)

    ntries = 1
    for eid in edgeids:
        newg = g.copy()
        newg.delete_edges([g.es[eid]])
        if newg.is_connected(mode='strong'):
            return newg, ntries
        ntries += 1
    raise Exception()

##########################################################
def simu_walk(idx, g, walklen, trimsz):
    """Walk with length @walklen in graph @g and update the @visits and
    @avgdgrees, in the positions given by @idx.
    The first @trimsz of the walk is disregarded."""
    adj = np.array(g.get_adjacency().data)
    trans = adj / np.sum(adj, axis=1).reshape(adj.shape[0], -1)
    startnode = np.random.randint(0, g.vcount())
    walk = randomwalk(walklen, startnode, trans)
    vs, cs = np.unique(walk[trimsz:], return_counts=True)
    visits = np.zeros(g.vcount(), dtype=int)
    for v, c in zip(vs, cs):
        visits[v] = c
    return visits

##########################################################
def run_experiment_lst(params):
    return run_experiment(*params)

##########################################################
def run_experiment(top, n, k, degmode, nbatches, batchsz,
        paired, trimrel, wepochs, fepochs, fthesh,
        eepochs, ei0, ebeta, egamma,
        outrootdir, seed):
    """Remove @batchsz arcs, @nbatches times and evaluate a walk of len
    @wepochs and the integrate-and-fire dynamics"""
        # params.append( [cfg.top, cfg.nvertices, cfg.avgdegree, cfg.degmode,
            # cfg.nbatches, cfg.batchsz, cfg.paired,
            # cfg.trimrel, cfg.wepochs, cfg.fepochs, cfg.fthresh,
            # cfg.eepochs, cfg.ei0rel, cfg.ebeta, cfg.egamma,
            # cfg.trimrel, cfg.outdir, seeds[i]] )
    np.random.seed(seed); random.seed(seed)

    outdir = pjoin(outrootdir, '{:02d}'.format(seed))
    os.makedirs(outdir, exist_ok=True)
    stronglyconn = False
    maxtries = 100
    tries = 0
    while not stronglyconn:
        gorig = generate_data(top, n, k)
        stronglyconn = gorig.is_connected(mode='strong')
        if tries > maxtries:
            raise Exception('Could not find strongly connected graph')
        tries += 1
    info('{} tries to generate a strongly connected graph'.format(tries))

    nattempts = np.zeros(nbatches + 1, dtype=int)
    nattempts[0] = tries

    plot_graph(gorig, top, outdir)
    gorig.to_directed()

    initial_check(nbatches, batchsz, gorig)
    g = gorig.copy()
    wtrim = int(wepochs * trimrel)
    ftrim = int(fepochs * trimrel)
    etrim = int(eepochs * trimrel)
    i0 = int(ei0*n)

    shp = (nbatches+1, g.vcount())
    err = - np.ones(shp, dtype=int)
    vvisit = np.zeros(shp, dtype=int) # Vertex visits
    vfires = np.zeros(shp, dtype=int) # Vertex fires
    vinfec = np.zeros(shp, dtype=int) # Vertex infections
    degrees = np.zeros(shp, dtype=int)
    lfires = - np.ones(nbatches + 1, dtype=int) # Last step fires
    linfec = - np.ones(nbatches + 1, dtype=int) # Last step inf

    degrees[0, :] = g.degree(mode=degmode)
    vvisit[0, :] = simu_walk(0, g, wepochs, wtrim)
    vfires[0, :], lfires[0] = simu_intandfire(g, fthesh, fepochs, ftrim)
    vinfec[0, :], linfec[0] = simu_sis(g, ebeta, egamma, i0, etrim, eepochs)

    for i in range(nbatches):
        info('Step {}'.format(i))
        # ntries = 0
        for _ in range(batchsz):
            try: g, m = remove_arc_conn(g)
            except: raise Exception('Could not remove arc in step {}'.format(i))
            nattempts[i+1] += m
        degrees[i+1, :] = g.degree(mode=degmode)
        vvisit[i+1, :] = simu_walk(i+1, g, wepochs, wtrim)
        vfires[i+1, :], lfires[i+1]  = simu_intandfire(g, fthesh, fepochs, ftrim)
        vinfec[i+1, :], linfec[i+1] = simu_sis(g, ebeta, egamma, i0, etrim, eepochs)

    np.save(pjoin(outdir, 'degrees.npy'), degrees)
    np.save(pjoin(outdir, 'vvisit.npy'), vvisit)
    np.save(pjoin(outdir, 'vfires.npy'), vfires)
    np.save(pjoin(outdir, 'vinfec.npy'), vinfec)
    np.save(pjoin(outdir, 'lfires.npy'), lfires)
    np.save(pjoin(outdir, 'linfec.npy'), linfec)
    np.save(pjoin(outdir, 'nattempts.npy'), nattempts)

    corrs = []
    for i in range(nbatches + 1): # nbatches
        # print(vvisit[i, :], vfires[i, :], vinfec[i, :],
                # degrees[i, :], i)
        c1, c2, c3 = calculate_correlations(vvisit[i, :], vfires[i, :], vinfec[i, :],
                degrees[i, :], i, outdir)
        corrs.append([top, g.vcount(), seed, i, c1, c2, c3])
    return corrs

##########################################################
def plot_graph(g, top, outdir):
    """Plot graph"""
    if top in ['gr', 'wx']:
        aux = np.array([ [g.vs['x'][i], g.vs['y'][i]] for i in range(g.vcount()) ])
    else:
        if top in ['la', 'ws']:
            layoutmodel = 'grid'
        else:
            layoutmodel = 'fr'
        aux = np.array(g.layout(layoutmodel).coords)
    coords = -1 + 2*(aux - np.min(aux, 0))/(np.max(aux, 0)-np.min(aux, 0)) # minmax

    f = pjoin(outdir, 'graph_und.png')
    igraph.plot(g, f, layout=coords.tolist())

##########################################################
def initial_check(nbatches, batchsz, g):
    """Check whether too many arcs are being removed."""
    if (nbatches * batchsz) > (0.75 * g.ecount()):
        info('Consider altering nbatches, batchsz, and avgdegree.')
        info('Execution may fail.')
        raise Exception('Too may arcs to be removed.')
    elif not g.is_connected(mode='strong'):
        raise Exception('Initial graph is not strongly connected.')

##########################################################
def plot_correlation_degree(meas, label, degrees, p, outpath):
    """Plot the number of visits by the degree for each vertex.
    Each dot represent an vertex"""
    # info(inspect.stack()[0][3] + '()')
    W = 640; H = 480
    fig, ax = plt.subplots(figsize=(W*.01, H*.01), dpi=100)
    ax.scatter(degrees, meas, alpha=.5)
    ax.set_title('Pearson {:.03f}'.format(p))
    ax.set_xlabel('Vertex degree')
    ax.set_ylabel(label)
    plt.savefig(outpath)
    plt.close()

##########################################################
def calculate_correlations(vvisits, vfires, vinfec, degrees, epoch, outdir):
    woutpath = pjoin(outdir, 'w_{:03d}.png'.format(epoch))
    foutpath = pjoin(outdir, 'f_{:03d}.png'.format(epoch))
    eoutpath = pjoin(outdir, 'e_{:03d}.png'.format(epoch))

    c1, c2, c3 = 0, 0, 0

    if np.sum(vvisits):
        wvisitsr = vvisits / np.sum(vvisits)
        c1 = pearsonr(degrees, wvisitsr)[0]
        t1 = 'Relative number of visits'
        plot_correlation_degree(wvisitsr, t1, degrees, c1, woutpath)

    if np.sum(vfires):
        nfiresr = vfires / np.sum(vfires)
        c2 = pearsonr(degrees, nfiresr)[0]
        t2 = 'Relative number of fires'
        plot_correlation_degree(nfiresr, t2, degrees, c2, foutpath)

    if np.sum(vinfec):
        ninfecr = vinfec / np.sum(vinfec)
        c3 = pearsonr(degrees, ninfecr)[0]
        t3 = 'Relative number of infections'
        plot_correlation_degree(ninfecr, t3, degrees, c3, eoutpath)

    return c1, c2, c3

#############################################################
def get_rgg_params(n, avgdegree):
    rggcatalog = {
        '600,6': [628, 0.0562]
    }

    k = '{},{}'.format(n, avgdegree)
    if k in rggcatalog.keys(): return rggcatalog[k]

    def f(r):
        g = igraph.Graph.GRG(n, r)
        return np.mean(g.degree()) - avgdegree

    return n, scipy.optimize.brentq(f, 0.0001, 10000)

##########################################################
def main(cfg, nprocs):
    np.random.seed(cfg.seed); random.seed(cfg.seed)
    stronglyconn = False
    maxtries = 100
    tries = 0

    retshp = (cfg.nrealizations, cfg.nbatches + 1, cfg.nvertices)
    seeds = [cfg.seed + i for i in range(cfg.nrealizations)]
    params = []
    for i in range(cfg.nrealizations):
        params.append( [cfg.top, cfg.nvertices, cfg.avgdegree, cfg.degmode,
            cfg.nbatches, cfg.batchsz, cfg.paired,
            cfg.trimrel, cfg.wepochs, cfg.fepochs, cfg.fthresh,
            cfg.eepochs, cfg.ei0, cfg.ebeta, cfg.egamma,
            cfg.outdir, seeds[i]] )

    if nprocs == 1:
        corrs = [ run_experiment_lst(p) for p in params ]
    else:
        info('Running in parallel ({})'.format(nprocs))
        pool = Pool(nprocs)
        corrs = pool.map(run_experiment_lst, params)

    data = []

    for i in range(cfg.nrealizations):
        for j in range(cfg.nbatches + 1):
            data.append(corrs[i][j])

    cols = ['top', 'n', 'realiz', 'epoch', 'corrvisits', 'corrfires', 'corrinfec']
    pd.DataFrame(data, columns=cols).to_csv(pjoin(cfg.outdir, 'corrs.csv'),
            index=False)

##########################################################
if __name__ == "__main__":
    info(datetime.date.today())
    t0 = time.time()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', help='config in json format')
    parser.add_argument('--nprocs', default=1, type=int,
            help='Number of processes')
    args = parser.parse_args()

    cfg = json.load(open(args.config), object_hook=lambda d: SimpleNamespace(**d))
    os.makedirs(cfg.outdir, exist_ok=True)
    readmepath = create_readme(sys.argv, cfg.outdir)
    shutil.copy(args.config, pjoin(cfg.outdir, 'config.json'))
    main(cfg, args.nprocs)

    info('Elapsed time:{:.02f}s'.format(time.time()-t0))
    info('Output generated in {}'.format(cfg.outdir))
