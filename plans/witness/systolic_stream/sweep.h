// Prototype: the streaming systolic sweep over banked operands (plans/systolic_characterization.md,
// Part B).  Complex, form 4.  X: [MMAX][KG][L] (row bank, lane bank), B: [KMAX][NT][C] (column bank).
#include <ap_int.h>
const int MMAX = 8, KMAX = 8, NMAX = 32, L = 4, R = 4, C = 8;
const int KG = (KMAX + L - 1) / L, NT = NMAX / C, CL = C / L, G = R * C / L;
typedef ap_int<12> a_t;
typedef ap_int<13> b_t;      // B in the array (one bit wider for the conjugate)
typedef ap_int<29> acc_t;

static inline int imax(int a, int b) { return a > b ? a : b; }

// The tile period: at least k steps, the G output groups, and L (a group's drain window).
static inline int period(int k) { return imax(imax(k, G), L); }

// out_r/out_i: C as [MMAX][NMAX] exact sums (the prototype checks the schedule, not the rounding).
void sweep(const a_t xr[MMAX][KG][L], const a_t xi[MMAX][KG][L],
           const b_t br[KMAX][NT][C], const b_t bi[KMAX][NT][C],
           acc_t out_r[MMAX][NMAX], acc_t out_i[MMAX][NMAX], int m, int k, int n);
