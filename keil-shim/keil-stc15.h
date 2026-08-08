/* Keil-name shim. SPDX-License-Identifier: MIT */
#ifndef _SHIM_KEIL_STC15_H
#define _SHIM_KEIL_STC15_H
#include <mcs51/stc12.h>  /* The STC15 map agrees with the STC12's except for the
                             additions in keil-compat-stc15.h -- crucially Timer 2,
                             which lives at T2H/T2L 0xD6/0xD7 with no T2CON. That
                             divergence is why stc15 headers were once deliberately
                             unmapped; a family of their own makes them safe. */
#include "keil-compat.h"
#include "keil-compat-stc12.h"
#include "keil-compat-stc15.h"
/* Vendor register headers routinely pull in intrins.h themselves;
   code downstream counts on that. */
#include "intrins.h"
#endif
