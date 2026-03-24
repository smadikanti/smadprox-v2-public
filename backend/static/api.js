/**
 * NoHuman - API Client
 * Handles auth, API calls, and token management.
 */

const API = {
  // Auth token management
  getToken() {
    return localStorage.getItem('nohuman_token');
  },

  setToken(token) {
    localStorage.setItem('nohuman_token', token);
  },

  clearToken() {
    localStorage.removeItem('nohuman_token');
  },

  isLoggedIn() {
    return !!this.getToken();
  },

  // Redirect to login if not authenticated
  requireAuth() {
    if (!this.isLoggedIn()) {
      window.location.href = '/';
      return false;
    }
    return true;
  },

  logout() {
    this.clearToken();
    window.location.href = '/';
  },

  // HTTP helpers
  async _fetch(url, options = {}) {
    const token = this.getToken();
    const headers = {
      'Content-Type': 'application/json',
      ...(token ? { 'Authorization': `Bearer ${token}` } : {}),
      ...options.headers,
    };

    const resp = await fetch(url, { ...options, headers });

    if (resp.status === 401) {
      this.clearToken();
      window.location.href = '/';
      throw new Error('Session expired');
    }

    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      throw new Error(body.detail || `Request failed: ${resp.status}`);
    }

    return resp.json();
  },

  async get(url) {
    return this._fetch(url);
  },

  async post(url, data) {
    return this._fetch(url, {
      method: 'POST',
      body: JSON.stringify(data),
    });
  },

  async patch(url, data) {
    return this._fetch(url, {
      method: 'PATCH',
      body: JSON.stringify(data),
    });
  },

  async del(url) {
    return this._fetch(url, { method: 'DELETE' });
  },

  // Auth
  async signup(email, password, name) {
    const result = await this.post('/api/auth/signup', { email, password, name });
    if (result.access_token) {
      this.setToken(result.access_token);
    }
    return result;
  },

  async login(email, password) {
    const result = await this.post('/api/auth/login', { email, password });
    if (result.access_token) {
      this.setToken(result.access_token);
    }
    return result;
  },

  async getMe() {
    return this.get('/api/auth/me');
  },

  // Contexts
  async listContexts() {
    return this.get('/api/contexts');
  },

  async createContext(data) {
    return this.post('/api/contexts', data);
  },

  async getContext(id) {
    return this.get(`/api/contexts/${id}`);
  },

  async updateContext(id, data) {
    return this.patch(`/api/contexts/${id}`, data);
  },

  async deleteContext(id) {
    return this.del(`/api/contexts/${id}`);
  },

  // Documents
  async listDocuments(contextId) {
    return this.get(`/api/contexts/${contextId}/documents`);
  },

  async createDocument(contextId, data) {
    return this.post(`/api/contexts/${contextId}/documents`, data);
  },

  async updateDocument(docId, data) {
    return this.patch(`/api/documents/${docId}`, data);
  },

  async deleteDocument(docId) {
    return this.del(`/api/documents/${docId}`);
  },

  // Sessions
  async createSession(contextId) {
    return this.post('/api/sessions', { context_id: contextId });
  },
};


// Toast notifications
function showToast(message, type = 'info') {
  const toast = document.createElement('div');
  toast.className = `toast ${type === 'error' ? 'toast-error' : type === 'success' ? 'toast-success' : ''}`;
  toast.textContent = message;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 3000);
}
