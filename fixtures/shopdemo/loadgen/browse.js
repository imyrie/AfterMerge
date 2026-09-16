// Steady browse traffic against the order listing.
//
// constant-arrival-rate (not constant-VUs) on purpose: it holds the REQUEST rate
// fixed even as responses slow down. A VU-based model would quietly send fewer
// requests once the N+1 lands, masking the regression in the very metric we're
// trying to measure.

import http from 'k6/http';
import { check } from 'k6';

const TARGET = __ENV.TARGET || 'http://gateway:8000';
const RPS = Number(__ENV.RPS || 20);
const DURATION = __ENV.DURATION || '60s';

export const options = {
  scenarios: {
    browse: {
      executor: 'constant-arrival-rate',
      rate: RPS,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: 20,
      // Generous headroom: at ~2s/request the bad build needs far more VUs to
      // sustain the same arrival rate.
      maxVUs: 300,
    },
  },
  // The bad build is *supposed* to breach these. Recorded, not enforced.
  thresholds: {},
};

export default function () {
  const res = http.get(`${TARGET}/orders?limit=50`);
  check(res, { 'status is 200': (r) => r.status === 200 });
}
