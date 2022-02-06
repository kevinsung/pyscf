#!/usr/bin/env python
# Copyright 2021-2022 The PySCF Developers. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Author: Qiming Sun <osirpt.sun@gmail.com>
#
# Ref:
# Chem Phys Lett, 256, 454
# J. Mol. Struct. THEOCHEM, 914, 3
# Recent Advances in Density Functional Methods, Chapter 5, M. E. Casida
#


from functools import reduce
import numpy
from pyscf import lib
from pyscf import dft
from pyscf.dft import numint
from pyscf import ao2mo
from pyscf import symm
from pyscf.lib import logger
from pyscf.tdscf import rhf
from pyscf.scf import ghf_symm
from pyscf.data import nist
from pyscf import __config__

OUTPUT_THRESHOLD = getattr(__config__, 'tdscf_rhf_get_nto_threshold', 0.3)
REAL_EIG_THRESHOLD = getattr(__config__, 'tdscf_rhf_TDDFT_pick_eig_threshold', 1e-4)

# Low excitation filter to avoid numerical instability
POSTIVE_EIG_THRESHOLD = getattr(__config__, 'tdscf_rhf_TDDFT_positive_eig_threshold', 1e-3)


def gen_tda_operation(mf, fock_ao=None, wfnsym=None):
    '''A x

    Kwargs:
        wfnsym : int or str
            Point group symmetry irrep symbol or ID for excited CIS wavefunction.
    '''
    mol = mf.mol
    mo_coeff = mf.mo_coeff
    mo_energy = mf.mo_energy
    mo_occ = mf.mo_occ
    nao, nmo = mo_coeff.shape
    occidx = numpy.where(mo_occ == 1)[0]
    viridx = numpy.where(mo_occ == 0)[0]
    nocc = len(occidx)
    nvir = len(viridx)
    orbv = mo_coeff[:,viridx]
    orbo = mo_coeff[:,occidx]

    if wfnsym is not None and mol.symmetry:
        if isinstance(wfnsym, str):
            wfnsym = symm.irrep_name2id(mol.groupname, wfnsym)
        wfnsym = wfnsym % 10  # convert to D2h subgroup
        orbsym = ghf_symm.get_orbsym(mol, mo_coeff)
        orbsym_in_d2h = numpy.asarray(orbsym) % 10  # convert to D2h irreps
        sym_forbid = (orbsym_in_d2h[occidx,None] ^ orbsym_in_d2h[viridx]) != wfnsym

    if fock_ao is None:
        #dm0 = mf.make_rdm1(mo_coeff, mo_occ)
        #fock_ao = mf.get_hcore() + mf.get_veff(mol, dm0)
        foo = numpy.diag(mo_energy[occidx])
        fvv = numpy.diag(mo_energy[viridx])
    else:
        fock = reduce(numpy.dot, (mo_coeff.conj().T, fock_ao, mo_coeff))
        foo = fock[occidx[:,None],occidx]
        fvv = fock[viridx[:,None],viridx]

    hdiag = fvv.diagonal() - foo.diagonal()[:,None]
    if wfnsym is not None and mol.symmetry:
        hdiag[sym_forbid] = 0
    hdiag = hdiag.ravel().real

    mo_coeff = numpy.asarray(numpy.hstack((orbo,orbv)), order='F')
    vresp = mf.gen_response(hermi=0)

    def vind(zs):
        zs = numpy.asarray(zs).reshape(-1,nocc,nvir)
        if wfnsym is not None and mol.symmetry:
            zs = numpy.copy(zs)
            zs[:,sym_forbid] = 0

        dmov = lib.einsum('xov,qv,po->xpq', zs, orbv.conj(), orbo)
        v1ao = vresp(dmov)
        v1ov = lib.einsum('xpq,qv,po->xov', v1ao, orbv, orbo.conj())
        v1ov += lib.einsum('xqs,sp->xqp', zs, fvv)
        v1ov -= lib.einsum('xpr,sp->xsr', zs, foo)
        if wfnsym is not None and mol.symmetry:
            v1ov[:,sym_forbid] = 0
        return v1ov.reshape(v1ov.shape[0],-1)

    return vind, hdiag
gen_tda_hop = gen_tda_operation

def get_ab(mf, mo_energy=None, mo_coeff=None, mo_occ=None):
    r'''A and B matrices for TDDFT response function.

    A[i,a,j,b] = \delta_{ab}\delta_{ij}(E_a - E_i) + (ia||bj)
    B[i,a,j,b] = (ia||jb)
    '''
    if mo_energy is None: mo_energy = mf.mo_energy
    if mo_coeff is None: mo_coeff = mf.mo_coeff
    if mo_occ is None: mo_occ = mf.mo_occ
    assert mo_coeff.dtype == numpy.double

    mol = mf.mol
    nao, nmo = mo_coeff.shape
    occidx = numpy.where(mo_occ==1)[0]
    viridx = numpy.where(mo_occ==0)[0]
    orbv = mo_coeff[:,viridx]
    orbo = mo_coeff[:,occidx]
    nvir = orbv.shape[1]
    nocc = orbo.shape[1]
    mo = numpy.hstack((orbo,orbv))
    moa = mo[:nao].copy()
    mob = mo[nao:].copy()
    orboa = orbo[:nao]
    orbob = orbo[nao:]
    nmo = nocc + nvir

    e_ia = lib.direct_sum('a-i->ia', mo_energy[viridx], mo_energy[occidx])
    a = numpy.diag(e_ia.ravel()).reshape(nocc,nvir,nocc,nvir)
    b = numpy.zeros_like(a)

    def add_hf_(a, b, hyb=1):
        eri_mo_aa  = ao2mo.general(mol, [orboa,moa,moa,moa], compact=False)
        eri_mo_aa += ao2mo.general(mol, [orbob,mob,mob,mob], compact=False)
        eri_mo_aa = eri_mo_aa.reshape(nocc,nmo,nmo,nmo)
        eri_mo  = ao2mo.general(mol, [orboa,moa,mob,mob], compact=False)
        eri_mo += ao2mo.general(mol, [orbob,mob,moa,moa], compact=False)
        eri_mo = eri_mo.reshape(nocc,nmo,nmo,nmo)
        eri_mo += eri_mo_aa
        a += numpy.einsum('iabj->iajb', eri_mo   [:nocc,nocc:,nocc:,:nocc])
        a -= numpy.einsum('ijba->iajb', eri_mo_aa[:nocc,:nocc,nocc:,nocc:]) * hyb
        b += numpy.einsum('iajb->iajb', eri_mo   [:nocc,nocc:,:nocc,nocc:])
        b -= numpy.einsum('jaib->iajb', eri_mo_aa[:nocc,nocc:,:nocc,nocc:]) * hyb

    if isinstance(mf, dft.KohnShamDFT):
        ni = mf._numint
        ni.libxc.test_deriv_order(mf.xc, 2, raise_error=True)
        if getattr(mf, 'nlc', '') != '':
            raise NotImplementedError

        if not mf.collinear:
            raise NotImplementedError

        omega, alpha, hyb = ni.rsh_and_hybrid_coeff(mf.xc, mol.spin)

        add_hf_(a, b, hyb)

        xctype = ni._xc_type(mf.xc)
        dm0 = mf.make_rdm1(mo_coeff, mo_occ)
        dm0a = dm0[:nao,:nao].real.copy()
        dm0b = dm0[nao:,nao:].real.copy()
        mem_now = lib.current_memory()[0]
        max_memory = max(2000, mf.max_memory*.8-mem_now)

        def get_mo_value(ao):
            if ao.ndim == 2:
                mo_a = lib.einsum('rp,pi->ri', ao, moa)
                mo_b = lib.einsum('rp,pi->ri', ao, mob)
                return mo_a[:,:nocc], mo_a[:,nocc:], mo_b[:,:nocc], mo_b[:,nocc:]
            else:
                mo_a = lib.einsum('xrp,pi->xri', ao, moa)
                mo_b = lib.einsum('xrp,pi->xri', ao, mob)
                return mo_a[:,:,:nocc], mo_a[:,:,nocc:], mo_b[:,:,:nocc], mo_b[:,:,nocc:]

        if xctype == 'LDA':
            ao_deriv = 0
            for ao, mask, weight, coords \
                    in ni.block_loop(mol, mf.grids, nao, ao_deriv, max_memory):
                rho0a = numint.eval_rho(mol, ao, dm0a, mask, xctype)
                rho0b = numint.eval_rho(mol, ao, dm0b, mask, xctype)
                fxc = ni.eval_xc(mf.xc, (rho0a,rho0b), 1, deriv=2)[2]
                u_u, u_d, d_d = fxc[0].T

                mo_oa, mo_va, mo_ob, mo_vb = get_mo_value(ao)
                rho_ov_a = numpy.einsum('ri,ra->ria', mo_oa.conj(), mo_va)
                rho_ov_b = numpy.einsum('ri,ra->ria', mo_ob.conj(), mo_vb)
                rho_vo_a = rho_ov_a.conj()
                rho_vo_b = rho_ov_b.conj()

                w_ov_a = numpy.einsum('ria,r->ria', u_u * rho_ov_a + u_d * rho_ov_b, weight)
                a += lib.einsum('ria,rjb->iajb', w_ov_a, rho_vo_a)
                b += lib.einsum('ria,rjb->iajb', w_ov_a, rho_ov_a)

                w_ov_b = numpy.einsum('ria,r->ria', u_d * rho_ov_a + d_d * rho_ov_b, weight)
                a += lib.einsum('ria,rjb->iajb', w_ov_b, rho_vo_b)
                b += lib.einsum('ria,rjb->iajb', w_ov_b, rho_ov_b)

        elif xctype == 'GGA':
            ao_deriv = 1
            for ao, mask, weight, coords \
                    in ni.block_loop(mol, mf.grids, nao, ao_deriv, max_memory):
                rho0a = numint.eval_rho(mol, ao, dm0a, mask, xctype)
                rho0b = numint.eval_rho(mol, ao, dm0b, mask, xctype)
                vxc, fxc = ni.eval_xc(mf.xc, (rho0a,rho0b), 0, deriv=2)[1:3]
                uu, ud, dd = vxc[1].T
                u_u, u_d, d_d = fxc[0].T
                u_uu, u_ud, u_dd, d_uu, d_ud, d_dd = fxc[1].T
                uu_uu, uu_ud, uu_dd, ud_ud, ud_dd, dd_dd = fxc[2].T

                mo_oa, mo_va, mo_ob, mo_vb = get_mo_value(ao)
                rho_ov_a = numpy.einsum('xri,ra->xria', mo_oa.conj(), mo_va[0])
                rho_ov_b = numpy.einsum('xri,ra->xria', mo_ob.conj(), mo_vb[0])
                rho_ov_a[1:4] += numpy.einsum('ri,xra->xria', mo_oa[0].conj(), mo_va[1:4])
                rho_ov_b[1:4] += numpy.einsum('ri,xra->xria', mo_ob[0].conj(), mo_vb[1:4])
                rho_vo_a = rho_ov_a.conj()
                rho_vo_b = rho_vo_a.conj()
                # sigma1 ~ \nabla(\rho_\alpha+\rho_\beta) dot \nabla(|b><j|) z_{bj}
                a0a1 = numpy.einsum('xr,xria->ria', rho0a[1:4], rho_ov_a[1:4])
                a0b1 = numpy.einsum('xr,xria->ria', rho0a[1:4], rho_ov_b[1:4])
                b0a1 = numpy.einsum('xr,xria->ria', rho0b[1:4], rho_ov_a[1:4])
                b0b1 = numpy.einsum('xr,xria->ria', rho0b[1:4], rho_ov_b[1:4])

                # aaaa
                w_ov = numpy.empty_like(rho_ov_a)
                w_ov[0]  = numpy.einsum('r,ria->ria', u_u, rho_ov_a[0])
                w_ov[0] += numpy.einsum('r,ria->ria', 2*u_uu, a0a1)
                w_ov[0] += numpy.einsum('r,ria->ria',   u_ud, b0a1)
                f_ov_a  = numpy.einsum('r,ria->ria', 4*uu_uu, a0a1)
                f_ov_b  = numpy.einsum('r,ria->ria', 2*uu_ud, a0a1)
                f_ov_a += numpy.einsum('r,ria->ria', 2*uu_ud, b0a1)
                f_ov_b += numpy.einsum('r,ria->ria',   ud_ud, b0a1)
                f_ov_a += numpy.einsum('r,ria->ria', 2*u_uu, rho_ov_a[0])
                f_ov_b += numpy.einsum('r,ria->ria',   u_ud, rho_ov_a[0])
                w_ov[1:] = numpy.einsum('ria,xr->xria', f_ov_a, rho0a[1:4])
                w_ov[1:]+= numpy.einsum('ria,xr->xria', f_ov_b, rho0b[1:4])
                w_ov[1:]+= numpy.einsum('r,xria->xria', 2*uu, rho_ov_a[1:4])
                w_ov *= weight[:,None,None]
                a += lib.einsum('xria,xrjb->iajb', w_ov, rho_vo_a)
                b += lib.einsum('xria,xrjb->iajb', w_ov, rho_ov_a)

                # bbbb
                w_ov = numpy.empty_like(rho_ov_b)
                w_ov[0]  = numpy.einsum('r,ria->ria', d_d, rho_ov_b[0])
                w_ov[0] += numpy.einsum('r,ria->ria', 2*d_dd, b0b1)
                w_ov[0] += numpy.einsum('r,ria->ria',   d_ud, a0b1)
                f_ov_b  = numpy.einsum('r,ria->ria', 4*dd_dd, b0b1)
                f_ov_a  = numpy.einsum('r,ria->ria', 2*ud_dd, b0b1)
                f_ov_b += numpy.einsum('r,ria->ria', 2*ud_dd, a0b1)
                f_ov_a += numpy.einsum('r,ria->ria',   ud_ud, a0b1)
                f_ov_b += numpy.einsum('r,ria->ria', 2*d_dd, rho_ov_b[0])
                f_ov_a += numpy.einsum('r,ria->ria',   d_ud, rho_ov_b[0])
                w_ov[1:] = numpy.einsum('ria,xr->xria', f_ov_a, rho0a[1:4])
                w_ov[1:]+= numpy.einsum('ria,xr->xria', f_ov_b, rho0b[1:4])
                w_ov[1:]+= numpy.einsum('r,xria->xria', 2*dd, rho_ov_b[1:4])
                w_ov *= weight[:,None,None]
                a += lib.einsum('xria,xrjb->iajb', w_ov, rho_vo_b)
                b += lib.einsum('xria,xrjb->iajb', w_ov, rho_ov_b)

                # aabb and bbaa
                w_ov = numpy.empty_like(rho_ov_b)
                w_ov[0]  = numpy.einsum('r,ria->ria', u_d, rho_ov_b[0])
                w_ov[0] += numpy.einsum('r,ria->ria', 2*u_dd, b0b1)
                w_ov[0] += numpy.einsum('r,ria->ria',   u_ud, a0b1)
                f_ov_a  = numpy.einsum('r,ria->ria', 4*uu_dd, b0b1)
                f_ov_b  = numpy.einsum('r,ria->ria', 2*ud_dd, b0b1)
                f_ov_a += numpy.einsum('r,ria->ria', 2*uu_ud, a0b1)
                f_ov_b += numpy.einsum('r,ria->ria',   ud_ud, a0b1)
                f_ov_a += numpy.einsum('r,ria->ria', 2*d_uu, rho_ov_b[0])
                f_ov_b += numpy.einsum('r,ria->ria',   d_ud, rho_ov_b[0])
                w_ov[1:] = numpy.einsum('ria,xr->xria', f_ov_a, rho0a[1:4])
                w_ov[1:]+= numpy.einsum('ria,xr->xria', f_ov_b, rho0b[1:4])
                w_ov[1:]+= numpy.einsum('r,xria->xria', ud, rho_ov_b[1:4])
                w_ov *= weight[:,None,None]
                a_iajb = lib.einsum('xria,xrjb->iajb', w_ov, rho_vo_a)
                b_iajb = lib.einsum('xria,xrjb->iajb', w_ov, rho_ov_a)
                a += a_iajb
                a += a_iajb.conj().transpose(2,3,0,1)
                b += b_iajb * 2

        elif xctype == 'HF':
            pass
        elif xctype == 'NLC':
            raise NotImplementedError('NLC')
        elif xctype == 'MGGA':
            raise NotImplementedError('meta-GGA')

    else:
        add_hf_(a, b)

    return a, b

def get_nto(tdobj, state=1, threshold=OUTPUT_THRESHOLD, verbose=None):
    raise NotImplementedError('get_nto')

def analyze(tdobj, verbose=None):
    raise NotImplementedError('analyze')

def _contract_multipole(tdobj, ints, hermi=True, xy=None):
    raise NotImplementedError


class TDMixin(rhf.TDMixin):

    @lib.with_doc(get_ab.__doc__)
    def get_ab(self, mf=None):
        if mf is None: mf = self._scf
        return get_ab(mf)

    analyze = analyze
    get_nto = get_nto
    _contract_multipole = _contract_multipole  # needed by transition dipoles

    def nuc_grad_method(self):
        raise NotImplementedError


@lib.with_doc(rhf.TDA.__doc__)
class TDA(TDMixin):

    singlet = None

    def gen_vind(self, mf):
        '''Compute Ax'''
        return gen_tda_hop(mf, wfnsym=self.wfnsym)

    def init_guess(self, mf, nstates=None, wfnsym=None):
        if nstates is None: nstates = self.nstates
        if wfnsym is None: wfnsym = self.wfnsym

        mo_energy = mf.mo_energy
        mo_occ = mf.mo_occ
        occidx = numpy.where(mo_occ==2)[0]
        viridx = numpy.where(mo_occ==0)[0]
        e_ia = mo_energy[viridx] - mo_energy[occidx,None]
        e_ia_max = e_ia.max()

        if wfnsym is not None and mf.mol.symmetry:
            if isinstance(wfnsym, str):
                wfnsym = symm.irrep_name2id(mf.mol.groupname, wfnsym)
            wfnsym = wfnsym % 10  # convert to D2h subgroup
            orbsym = ghf_symm.get_orbsym(mf.mol, mf.mo_coeff)
            orbsym_in_d2h = numpy.asarray(orbsym) % 10  # convert to D2h irreps
            e_ia[(orbsym_in_d2h[occidx,None] ^ orbsym_in_d2h[viridx]) != wfnsym] = 1e99

        nov = e_ia.size
        nstates = min(nstates, nov)
        e_ia = e_ia.ravel()
        e_threshold = min(e_ia_max, e_ia[numpy.argsort(e_ia)[nstates-1]])
        # Handle degeneracy, include all degenerated states in initial guess
        e_threshold += 1e-6

        idx = numpy.where(e_ia <= e_threshold)[0]
        x0 = numpy.zeros((idx.size, nov))
        for i, j in enumerate(idx):
            x0[i, j] = 1  # Koopmans' excitations
        return x0

    def kernel(self, x0=None, nstates=None):
        '''TDA diagonalization solver
        '''
        cpu0 = (logger.process_clock(), logger.perf_counter())
        self.check_sanity()
        self.dump_flags()
        if nstates is None:
            nstates = self.nstates
        else:
            self.nstates = nstates

        log = logger.Logger(self.stdout, self.verbose)

        vind, hdiag = self.gen_vind(self._scf)
        precond = self.get_precond(hdiag)

        if x0 is None:
            x0 = self.init_guess(self._scf, self.nstates)

        def pickeig(w, v, nroots, envs):
            idx = numpy.where(w > POSTIVE_EIG_THRESHOLD**2)[0]
            return w[idx], v[:,idx], idx

        # FIXME: Is it correct to call davidson1 for complex integrals
        self.converged, self.e, x1 = \
                lib.davidson1(vind, x0, precond,
                              tol=self.conv_tol,
                              nroots=nstates, lindep=self.lindep,
                              max_cycle=self.max_cycle,
                              max_space=self.max_space, pick=pickeig,
                              verbose=log)

        nocc = (self._scf.mo_occ>0).sum()
        nmo = self._scf.mo_occ.size
        nvir = nmo - nocc
        self.xy = [(xi.reshape(nocc,nvir), 0) for xi in x1]

        if self.chkfile:
            lib.chkfile.save(self.chkfile, 'tddft/e', self.e)
            lib.chkfile.save(self.chkfile, 'tddft/xy', self.xy)

        log.timer('TDA', *cpu0)
        self._finalize()
        return self.e, self.xy

CIS = TDA


def gen_tdhf_operation(mf, fock_ao=None, wfnsym=None):
    '''Generate function to compute

    [ A   B ][X]
    [-B* -A*][Y]
    '''
    mol = mf.mol
    mo_coeff = mf.mo_coeff
    mo_energy = mf.mo_energy
    mo_occ = mf.mo_occ
    nao, nmo = mo_coeff.shape
    occidx = numpy.where(mo_occ == 1)[0]
    viridx = numpy.where(mo_occ == 0)[0]
    nocc = len(occidx)
    nvir = len(viridx)
    orbv = mo_coeff[:,viridx]
    orbo = mo_coeff[:,occidx]

    if wfnsym is not None and mol.symmetry:
        if isinstance(wfnsym, str):
            wfnsym = symm.irrep_name2id(mol.groupname, wfnsym)
        wfnsym = wfnsym % 10  # convert to D2h subgroup
        orbsym = ghf_symm.get_orbsym(mol, mo_coeff)
        orbsym_in_d2h = numpy.asarray(orbsym) % 10  # convert to D2h irreps
        sym_forbid = (orbsym_in_d2h[occidx,None] ^ orbsym_in_d2h[viridx]) != wfnsym

    #dm0 = mf.make_rdm1(mo_coeff, mo_occ)
    #fock_ao = mf.get_hcore() + mf.get_veff(mol, dm0)
    #fock = reduce(numpy.dot, (mo_coeff.T, fock_ao, mo_coeff))
    #foo = fock[occidx[:,None],occidx]
    #fvv = fock[viridx[:,None],viridx]
    foo = numpy.diag(mo_energy[occidx])
    fvv = numpy.diag(mo_energy[viridx])

    hdiag = fvv.diagonal() - foo.diagonal()[:,None]
    if wfnsym is not None and mol.symmetry:
        hdiag[sym_forbid] = 0
    hdiag = numpy.hstack((hdiag.ravel(), -hdiag.ravel())).real

    mo_coeff = numpy.asarray(numpy.hstack((orbo,orbv)), order='F')
    vresp = mf.gen_response(hermi=0)

    def vind(xys):
        xys = numpy.asarray(xys).reshape(-1,2,nocc,nvir)
        if wfnsym is not None and mol.symmetry:
            # shape(nz,2,nocc,nvir): 2 ~ X,Y
            xys = numpy.copy(xys)
            xys[:,:,sym_forbid] = 0

        xs, ys = xys.transpose(1,0,2,3)
        # dms = AX + BY
        dms  = lib.einsum('xov,qv,po->xpq', xs, orbv.conj(), orbo)
        dms += lib.einsum('xov,pv,qo->xpq', ys, orbv, orbo.conj())

        v1ao = vresp(dms)
        v1ov = lib.einsum('xpq,po,qv->xov', v1ao, orbo.conj(), orbv)
        v1vo = lib.einsum('xpq,qo,pv->xov', v1ao, orbo, orbv.conj())
        v1ov += lib.einsum('xqs,sp->xqp', xs, fvv)  # AX
        v1ov -= lib.einsum('xpr,sp->xsr', xs, foo)  # AX
        v1vo += lib.einsum('xqs,sp->xqp', ys, fvv)  # AY
        v1vo -= lib.einsum('xpr,sp->xsr', ys, foo)  # AY

        if wfnsym is not None and mol.symmetry:
            v1ov[:,sym_forbid] = 0
            v1vo[:,sym_forbid] = 0

        # (AX, (-A*)Y)
        nz = xys.shape[0]
        hx = numpy.hstack((v1ov.reshape(nz,-1), -v1vo.reshape(nz,-1)))
        return hx

    return vind, hdiag


class TDHF(TDMixin):

    singlet = None

    @lib.with_doc(gen_tdhf_operation.__doc__)
    def gen_vind(self, mf):
        return gen_tdhf_operation(mf, wfnsym=self.wfnsym)

    def init_guess(self, mf, nstates=None, wfnsym=None):
        x0 = TDA.init_guess(self, mf, nstates, wfnsym)
        y0 = numpy.zeros_like(x0)
        return numpy.hstack((x0,y0))

    def kernel(self, x0=None, nstates=None):
        '''TDHF diagonalization with non-Hermitian eigenvalue solver
        '''
        cpu0 = (logger.process_clock(), logger.perf_counter())
        self.check_sanity()
        self.dump_flags()
        if nstates is None:
            nstates = self.nstates
        else:
            self.nstates = nstates

        log = logger.Logger(self.stdout, self.verbose)

        vind, hdiag = self.gen_vind(self._scf)
        precond = self.get_precond(hdiag)
        if x0 is None:
            x0 = self.init_guess(self._scf, self.nstates)

        def pickeig(w, v, nroots, envs):
            realidx = numpy.where((abs(w.imag) < REAL_EIG_THRESHOLD) &
                                  (w.real > POSTIVE_EIG_THRESHOLD))[0]
            # FIXME: Should the amplitudes be real? It also affects x2c-tdscf
            return lib.linalg_helper._eigs_cmplx2real(w, v, realidx,
                                                      real_eigenvectors=False)

        self.converged, w, x1 = \
                lib.davidson_nosym1(vind, x0, precond,
                                    tol=self.conv_tol,
                                    nroots=nstates, lindep=self.lindep,
                                    max_cycle=self.max_cycle,
                                    max_space=self.max_space, pick=pickeig,
                                    verbose=log)

        nocc = (self._scf.mo_occ>0).sum()
        nmo = self._scf.mo_occ.size
        nvir = nmo - nocc
        self.e = w
        def norm_xy(z):
            x, y = z.reshape(2,nocc,nvir)
            norm = lib.norm(x)**2 - lib.norm(y)**2
            norm = numpy.sqrt(1./norm)
            return x*norm, y*norm
        self.xy = [norm_xy(z) for z in x1]

        if self.chkfile:
            lib.chkfile.save(self.chkfile, 'tddft/e', self.e)
            lib.chkfile.save(self.chkfile, 'tddft/xy', self.xy)

        log.timer('TDDFT', *cpu0)
        self._finalize()
        return self.e, self.xy

RPA = TDGHF = TDHF

from pyscf import scf
scf.ghf.GHF.TDA = lib.class_as_method(TDA)
scf.ghf.GHF.TDHF = lib.class_as_method(TDHF)