/* Keil-name shim. SPDX-License-Identifier: MIT */
#ifndef _SHIM_KEIL_REG52_H
#define _SHIM_KEIL_REG52_H
#include <mcs51/8052.h>   /* mcs51/ prefix: a project header must not shadow SDCC's.
                             8052.h, not stc12.h: reg52 code expects Timer 2, which
                             the STC12C5A60S2 does not have, and the STC89's
                             WDT_CONTR/P4/ISP_* sit at different addresses. */
#include "keil-compat.h"
#include "keil-compat-8052.h"
/* Vendor register headers routinely pull in intrins.h themselves;
   code downstream counts on that. */
#include "intrins.h"
#endif
