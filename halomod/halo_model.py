#===============================================================================
# Some Imports
#===============================================================================
from scipy.interpolate import InterpolatedUnivariateSpline as spline
import scipy.integrate as intg
import numpy as np
from scipy.optimize import minimize
# import scipy.special as sp

from hmf import MassFunction
from hmf._cache import cached_property, parameter
# import hmf.tools as ht
import tools
from profiles import get_profile
import hod
from bias import get_bias, tinker10
from fort.routines import hod_routines as fort
from twohalo_wrapper import twohalo_wrapper as thalo

from copy import deepcopy

USEFORT = True
#===============================================================================
# The class itself
#===============================================================================
class HaloModel(MassFunction):
    '''
    Calculates several quantities using the halo model.

    Parameters
    ----------
    r : array_like, optional, default ``np.logspace(-2.0,1.5,100)`` 
        The scales at which the correlation function is calculated in Mpc/*h*

    **kwargs: anything that can be used in the MassFunction class
    
    '''
    rlog = True

    def __init__(self, rmin=0.1, rmax=50.0, rnum=20, hod_params={},
                 hod_model=hod.Zehavi05,
                 halo_profile='NFW', cm_relation='duffy', bias_model=tinker10,
                 nonlinear=True, scale_dependent_bias=True,
                 halo_exclusion="None", ng=None, nthreads_2halo=0, ** hmf_kwargs):

        # Pre-process Some Arguments
        if "cut_fit" not in hmf_kwargs:
            hmf_kwargs.update({"cut_fit":False})

        # Do Mass Function __init__ MUST BE DONE FIRST (to init Cache)
        super(HaloModel, self).__init__(**hmf_kwargs)

        # Initially save parameters to the class.
        self.hod_params = hod_params
        self.hod_model = hod_model
        self.halo_profile = halo_profile
        self.cm_relation = cm_relation
        self.bias_model = bias_model
        self.rmin = rmin
        self.rmax = rmax
        self.rnum = rnum
        self.nonlinear = nonlinear
        self.halo_exclusion = halo_exclusion
        self.scale_dependent_bias = scale_dependent_bias

        self.nthreads_2halo = nthreads_2halo

        # A special argument, making it possible to define M_min by mean density
        self.ng = ng

        # Find mmin if we want to
        if ng is not None:
            mmin = self._find_m_min(ng)
            self.hod_params = {"M_min":mmin}


    def update(self, **kwargs):
        """
        Updates any parameter passed
        """
        if "ng" in kwargs:
            self.ng = kwargs.pop('ng')
        elif "hod_params" in kwargs:
            if "M_min" in kwargs["hod_params"]:
                self.ng = None

        super(HaloModel, self).update(**kwargs)

        if self.ng is not None:
            mmin = self._find_m_min(self.ng)
            self.hod_params = {"M_min":mmin}

#===============================================================================
# Parameters
#===============================================================================
    @parameter
    def ng(self, val):
        """Mean density of galaxies, ONLY if passed directly"""
        return val

    @parameter
    def hod_params(self, val):
        """Dictionary of parameters for the HOD model"""
        return val

    @parameter
    def hod_model(self, val):
        """:class:`hod.hod.HOD` class"""
        if not isinstance(val, basestring):

            if not issubclass(val, hod.HOD):
                raise ValueError("hod_model must be a subclass of hod.HOD")
            else:
                return val
        else:
            return hod.get_hod(val)

    @parameter
    def nonlinear(self, val):
        """Logical indicating whether the power is to be nonlinear or not"""
        try:
            if val:
                return True
            else:
                return False
        except:
            raise ValueError("nonlinear must be a logical value")

    @parameter
    def halo_profile(self, val):
        """A string identifier for the halo density profile used"""
        return val

    @parameter
    def cm_relation(self, val):
        available = ['duffy', 'zehavi', "bullock_rescaled"]
        if val not in available:
            if isinstance(val, str):
                raise ValueError("cm_relation not acceptable: " + str(val))

        return val

    @parameter
    def bias_model(self, val):
        return val

    @parameter
    def halo_exclusion(self, val):
        """A string identifier for the type of halo exclusion used (or None)"""
        if val is None:
            val = "None"
        available = ["None", "sphere", "ellipsoid", "ng_matched", 'schneider']
        if val not in available:
            raise ValueError("halo_exclusion not acceptable: " + str(val) + " " + str(type(val)))
        else:
            return val

    @parameter
    def rmin(self, val):
        return val

    @parameter
    def rmax(self, val):
        return val

    @parameter
    def rnum(self, val):
        return val

    @parameter
    def scale_dependent_bias(self, val):
        try:
            if val:
                return True
            else:
                return False
        except:
            raise ValueError("scale_dependent_bias must be a boolean/have logical value")
#===============================================================================
# Start the actual calculations
#===============================================================================
    @cached_property("rmin", "rmax", "rnum")
    def r(self):
        if self.rlog:
            return np.exp(np.linspace(np.log(self.rmin), np.log(self.rmax), self.rnum))
        else:
            return np.linspace(self.rmin, self.rmax, self.rnum)

    @cached_property("hod_model", "hod_params")
    def hod(self):
        return self.hod_model(**self.hod_params)

    @cached_property("hod", "dlog10m")
    def M(self):
        return 10 ** np.arange(self.hod.mmin, 18, self.dlog10m)

    @cached_property("hod", "M")
    def n_sat(self):
        """Average satellite occupancy of halo of mass M"""
        return self.hod.ns(self.M)

    @cached_property("hod", "M")
    def n_cen(self):
        """Average satellite occupancy of halo of mass M"""
        return self.hod.nc(self.M)

    @cached_property("hod", "M")
    def n_tot(self):
        """Average satellite occupancy of halo of mass M"""
        return self.hod.ntot(self.M)

    @cached_property("bias_model", "sigma")
    def bias(self):
        """A class containing the elements necessary to calculate the halo bias"""
        try:
            return self.bias_model(self)
        except:
            return get_bias(self.bias_model)(self)

    @cached_property("halo_profile", "delta_halo", "cm_relation", "z", "omegam", "omegav")
    def profile(self):
        """A class containing the elements necessary to calculate halo profile quantities"""
        if hasattr(self.halo_profile, "rho"):
            return self.halo_profile
        else:
            return get_profile(self.halo_profile,
                               self.delta_halo,
                               cm_relation=self.cm_relation,
                               z=self.z,
                               truncate=True,
                               omegam=self.omegam,
                               omegav=self.omegav)

    @cached_property("dndm", "n_tot")
    def n_gal(self):
        """
        The total number density of galaxies in halos of mass M
        """
        return self.dndm * self.n_tot

    @cached_property("M", "dndm", "n_tot", "ng")
    def mean_gal_den(self):
        """
        The mean number density of galaxies
        """
        if self.ng is not None:
            return self.ng
        else:
#             Integrand is just the density of galaxies at mass M
            integrand = self.M * self.dndm * self.n_tot
        return intg.simps(integrand, dx=np.log(self.M[1]) - np.log(self.M[0]),
                          even="first")


    @cached_property("M", "dndm", "n_tot", "bias")
    def bias_effective(self):
        """
        The galaxy number weighted halo bias factor (Tinker 2005)
        """
        # Integrand is just the density of galaxies at mass M by bias
        integrand = self.M * self.dndm * self.n_tot * self.bias
        b = intg.simps(integrand, dx=np.log(self.M[1]) - np.log(self.M[0]))

        return b / self.mean_gal_den

    @cached_property("M", 'dndm', 'n_tot', "mean_gal_den")
    def mass_effective(self):
        """
        Average group halo mass, or host-halo mass (in log10 units)
        """
        # Integrand is just the density of galaxies at mass M by M
        integrand = self.M ** 2 * self.dndm * self.n_tot

        m = intg.simps(integrand, dx=np.log(self.M[1]) - np.log(self.M[0]))
        return np.log10(m / self.mean_gal_den)

    @cached_property("M", "dndm", "n_sat", "mean_gal_den")
    def satellite_fraction(self):
        # Integrand is just the density of satellite galaxies at mass M
        integrand = self.M * self.dndm * self.n_sat
        s = intg.simps(integrand, dx=np.log(self.M[1]) - np.log(self.M[0]))
        return s / self.mean_gal_den

    @cached_property("satellite_fraction")
    def central_fraction(self):
        return 1 - self.satellite_fraction

    @cached_property("nonlinear", "power", "nonlinear_power")
    def matter_power(self):
        """The matter power used in calculations -- can be linear or nonlinear
        
        .. note :: Linear power is available through :attr:`.power`
        """
        if self.nonlinear:
            return self.nonlinear_power
        else:
            return self.power

    @cached_property("matter_power", 'lnk', 'r')
    def dm_corr(self):
        """
        The dark-matter-only two-point correlation function of the given cosmology
        """
        return tools.power_to_corr_ogata(np.exp(self.matter_power),
                                         self.lnk, self.r)

    @cached_property("lnk", "M", "dndm", "n_sat", "n_cen", 'hod', 'profile', "mean_gal_den")
    def _power_gal_1h_ss(self):
        """
        The sat-sat part of the 1-halo term of the galaxy power spectrum
        """
        u = self.profile.u(np.exp(self.lnk), self.M, norm='m')
        p = fort.power_gal_1h_ss(nlnk=len(self.lnk),
                                 nm=len(self.M),
                                 u=np.asfortranarray(u),
                                 dndm=self.dndm,
                                 nsat=self.n_sat,
                                 ncen=self.n_cen,
                                 mass=self.M,
                                 central=self.hod._central)
        return p / self.mean_gal_den ** 2

    @cached_property("_power_gal_1h_ss", "lnk", "r")
    def _corr_gal_1h_ss(self):
        return tools.power_to_corr_ogata(self._power_gal_1h_ss,
                                         self.lnk, self.r)

    @cached_property("r", "M", "dndm", "n_cen", "n_sat", "mean_dens", "delta_halo", "mean_gal_den")
    def _corr_gal_1h_cs(self):
        """The cen-sat part of the 1-halo galaxy correlations"""
        rho = self.profile.rho(self.r, self.M, norm="m")
        c = fort.corr_gal_1h_cs(nr=len(self.r),
                                nm=len(self.M),
                                r=self.r,
                                mass=self.M,
                                dndm=self.dndm,
                                ncen=self.n_cen,
                                nsat=self.n_sat,
                                rho=np.asfortranarray(rho),
                                mean_dens=self.mean_dens,
                                delta_halo=self.delta_halo)
        return c / self.mean_gal_den ** 2

    @cached_property("r", "M", "dndm", "n_cen", "n_sat", "hod", "mean_dens", "delta_halo",
                     "mean_gal_dens", "_corr_gal_1h_cs", "_corr_gal_1h_ss")
    def corr_gal_1h(self):
        """The 1-halo term of the galaxy correlations"""
        if self.profile.has_lam:
            rho = self.profile.rho(self.r, self.M, norm="m")
            lam = self.profile.lam(self.r, self.M)
            c = fort.corr_gal_1h(nr=len(self.r),
                                 nm=len(self.M),
                                 r=self.r,
                                 mass=self.M,
                                 dndm=self.dndm,
                                 ncen=self.n_cen,
                                 nsat=self.n_sat,
                                 rho=np.asfortranarray(rho),
                                 lam=np.asfortranarray(lam),
                                 central=self.hod._central,
                                 mean_dens=self.mean_dens,
                                 delta_halo=self.delta_halo)

            return c / self.mean_gal_den ** 2

        else:
            return self._corr_gal_1h_cs + self._corr_gal_1h_ss

    @cached_property("profile", "lnk", "M", "halo_exclusion", "scale_dependent_bias",
                     "bias", "n_tot", 'dndm', "matter_power", "r", "dm_corr",
                     "mean_gal_den", "delta_halo", "mean_dens")
    def corr_gal_2h(self):
        """The 2-halo term of the galaxy correlation"""
        u = self.profile.u(np.exp(self.lnk), self.M , norm='m')
        return thalo(self.halo_exclusion, self.scale_dependent_bias,
                     self.M, self.bias, self.n_tot,
                     self.dndm, self.lnk,
                     np.exp(self.matter_power), u, self.r, self.dm_corr,
                     self.mean_gal_den, self.delta_halo,
                     self.mean_dens, self.nthreads_2halo)

    @cached_property("corr_gal_1h", "corr_gal_2h")
    def  corr_gal(self):
        """The galaxy correlation function"""
        return self.corr_gal_1h + self.corr_gal_2h

    def _find_m_min(self, ng):
        """
        Calculate the minimum mass of a halo to contain a (central) galaxy 
        based on a known mean galaxy density
        """

        self.power  # This just makes sure the power is gotten and copied
        c = deepcopy(self)
        c.update(hod_params={"M_min":8}, dlog10m=0.01)

        integrand = c.M * c.dndm * c.n_tot

        if self.hod.sharp_cut:
            integral = intg.cumtrapz(integrand[::-1], dx=np.log(c.M[1]) - np.log(c.M[0]))

            if integral[-1] < ng:
                raise NGException("Maximum mean galaxy density exceeded: " + str(integral[-1]))

            ind = np.where(integral > ng)[0][0]

#             c.update(hod_params={"M_min":np.log10(c.M[::-1][ind + 1])}, dlogm=0.005)
#
#
#             print "ROUGH LOWER BIT: ", np.log10(c.M[::-1][ind + 1])
#             print "INTEGRAL HERE: ", integral[ind + 1], ng
#             print "NEW LENGTH OF M: ", len(c.M)
#
#             integrand = c.M * c.dndm * c.n_tot
#             integral = intg.cumtrapz(integrand[::-1], dx=np.log(c.M[1]) - np.log(c.M[0]))
#
#             print integral[0], integral[-1]
#             ind = np.where(integral > ng)[0][0]
#
#             print "INTEGRAL HERE: ", integral[ind], ng

            m = c.M[::-1][1:][max(ind - 4, 0):min(ind + 4, len(c.M))]
            integral = integral[max(ind - 4, 0):min(ind + 4, len(c.M))]


            spline_int = spline(np.log(integral), np.log(m), k=3)
            mmin = spline_int(np.log(ng)) / np.log(10)
        else:
            # Anything else requires us to do some optimization unfortunately.
            integral = intg.simps(integrand, dx=np.log(c.M[1]) - np.log(c.M[0]))
            if integral < ng:
                raise NGException("Maximum mean galaxy density exceeded: " + str(integral))

            def model(mmin):
                c.update(hod_params={"M_min":mmin})
                integrand = c.M * c.dndm * c.n_tot
                integral = intg.simps(integrand, dx=np.log(c.M[1]) - np.log(c.M[0]))
                return abs(integral - ng)

            res = minimize(model, 12.0, tol=1e-3,
                           method="Nelder-Mead", options={"maxiter":200})
            mmin = res.x[0]

        return mmin

    @cached_property("r", "corr_gal")
    def projected_corr_gal(self):
        """
        Projected correlation function w(r_p).

        From Beutler 2011, eq 6.

        To integrate perform a substitution y = x - r_p.
        """
        # We make a copy of the current instance but increase the number of
        # r and extend the range

    #    cr_max = max(80.0, 5 * self.r.max())

        # This is a bit of a hack, but make sure self has all parent attributes
        # self.dndm; self.matter_power
        self.dndm
        self.matter_power

        # c = deepcopy(self)
#
   #     rnum = int((np.log10(cr_max) - np.log10(self.r.min())) / 0.05) + 1
        # c.update(rmin=self.r.min(), rmax=cr_max, rnum=rnum)


  #      print "corr_gal: ", c.corr_gal[10:]
        # fit = spline(np.log(c.r[c.corr_gal > 0]), np.log(c.corr_gal[c.corr_gal > 0]), k=3)
        fit = spline(np.log(self.r[self.corr_gal > 0]), np.log(self.corr_gal[self.corr_gal > 0]), k=3)
        p = np.zeros(len(self.r))

        for i, rp in enumerate(self.r):
            # # Get steepest slope.
            ydiff = fit.derivatives(np.log(rp))[1]
            a = max(1.3, -ydiff)
            frac = self._get_slope_frac(a)
            min_y = frac * 0.005 ** 2 * rp  # 2.5% accuracy??

            # Set the y vector for this rp
            y = np.logspace(np.log10(min_y), np.log10(max(80.0, 5 * rp) - rp), 1000)

            # Integrate
            integ_corr = np.exp(fit(np.log(y + rp)))
            integrand = integ_corr * (y + rp) / np.sqrt((y + 2 * rp) * y)
            p[i] = intg.simps(integrand, y) * 2

        return p

    def _get_slope_frac(self, a):
        frac = 2 ** (1 + 2 * a) * (7 - 2 * a ** 3 + 3 * np.sqrt(5 - 8 * a + 4 * a ** 2) + a ** 2 * (9 + np.sqrt(5 - 8 * a + 4 * a ** 2)) -
                           a * (13 + 3 * np.sqrt(5 - 8 * a + 4 * a ** 2))) * ((1 + np.sqrt(5 - 8 * a + 4 * a ** 2)) / (a - 1)) ** (-2 * a)
        frac /= (a - 1) ** 2 * (-1 + 2 * a + np.sqrt(5 - 8 * a + 4 * a ** 2))
        return frac


class NGException(Exception):
    pass
