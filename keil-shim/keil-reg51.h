/* Keil-name shim. SPDX-License-Identifier: MIT */
#ifndef _SHIM_KEIL_REG51_H
#define _SHIM_KEIL_REG51_H
#include <mcs51/8051.h>   /* mcs51/ prefix: a project header must not shadow SDCC's.
                             8051.h, not stc12.h: this family's WDT_CONTR/P4/ISP_*
                             sit at different addresses than the STC12's. */
#include "keil-compat.h"
#include "keil-compat-8052.h"
/* Vendor register headers routinely pull in intrins.h themselves;
   code downstream counts on that. */
#include "intrins.h"
#endif
