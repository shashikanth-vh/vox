// The workflow plane — {{orchestratorUrl}} (env.json), which the gateway serves under
// the /orchestrator prefix of the same door as the Register. It needs its own axios
// instance because axiosClient is pinned to the Register's base URL.
//
// The timeout is longer than the Register's: these endpoints start Temporal workflows
// rather than write a row, so they answer more slowly.

import axios from 'axios';
import { authHeaders } from '../auth/session';
import { ORCHESTRATOR_URL } from './axiosClient';

const orchestratorClient = axios.create({
  baseURL: ORCHESTRATOR_URL,
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
});

// Same identity as every other call — read per-request so a sign-in takes effect at once.
orchestratorClient.interceptors.request.use((cfg) => {
  Object.entries(authHeaders()).forEach(([k, v]) => cfg.headers.set(k, v));
  return cfg;
});

export const orchestrator = {
  post: <T>(url: string, data?: any) => orchestratorClient.post<T>(url, data).then((r) => r.data),
  get: <T>(url: string, params?: Record<string, any>) => orchestratorClient.get<T>(url, { params }).then((r) => r.data),
  patch: <T>(url: string, data?: any) => orchestratorClient.patch<T>(url, data).then((r) => r.data),
};

export default orchestratorClient;
