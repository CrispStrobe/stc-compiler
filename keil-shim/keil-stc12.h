/* Keil-name shim. SPDX-License-Identifier: MIT */
#ifndef _SHIM_KEIL_STC12_H
#define _SHIM_KEIL_STC12_H
#include <mcs51/stc12.h>  /* mcs51/ prefix: a project header named stc12.h (or STC12.h on a
                             case-insensitive filesystem) must not shadow SDCC's */
#include "keil-compat.h"
/* Vendor register headers routinely pull in intrins.h themselves;
   code downstream counts on that. */
#include "intrins.h"
#endif
