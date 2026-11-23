/* BotoKo RPC client stub (JSON-RPC 2.0).
 *
 *   rpc('get_results').then(function (results) { ... });
 *   rpc('cast_vote', { choices: { 1: 3, 2: null } })
 *     .then(function (r) { ... r.receipt_code ... })
 *     .catch(function (err) { ... err.code (e.g. -32011 = already voted), err.message ... });
 *
 * Sends {"jsonrpc":"2.0","method":...,"params":...,"id":n} to POST /rpc with the login cookie and the
 * page's CSRF token (X-CSRF-Token header). Resolves with "result", or rejects with an RpcError
 * (code + message from the server, or code 0 if the server couldn't be reached).
 * rpc.lastMeta = which worker process (PID) and thread ran the last procedure.
 */
(function () {
  'use strict';
  var ENDPOINT = '/rpc';
  var nextId = 1;

  function RpcError(code, message, data, meta) {
    this.name = 'RpcError';
    this.code = code;
    this.message = message;
    this.data = data;
    this.meta = meta;
  }
  RpcError.prototype = Object.create(Error.prototype);
  RpcError.prototype.constructor = RpcError;

  // Same codes as rpc.py
  var CODES = {
    PARSE_ERROR: -32700, INVALID_REQUEST: -32600, METHOD_NOT_FOUND: -32601, INVALID_PARAMS: -32602,
    INTERNAL_ERROR: -32603, NOT_LOGGED_IN: -32001, ADMIN_ONLY: -32002, CSRF_FAILED: -32003,
    VOTING_CLOSED: -32010, ALREADY_VOTED: -32011, INVALID_VOTE: -32012, NOT_FOUND: -32013,
    PASSWORD_CHANGE_REQUIRED: -32014, NOT_REGISTERED: -32015, NOT_STARTED: -32016, NO_EVENT: -32017,
    REGISTRATION_REFUSED: -32018, TOO_MANY_ATTEMPTS: -32019, ACCOUNT_NOT_VERIFIED: -32020,
    RESULTS_NOT_PUBLISHED: -32021, NETWORK: 0
  };

  function csrfToken() {
    var m = document.querySelector('meta[name="csrf-token"]');
    return m ? m.getAttribute('content') : '';
  }

  function rpc(method, params) {
    var id = nextId++;
    return fetch(ENDPOINT, {
      method: 'POST',
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() },
      body: JSON.stringify({ jsonrpc: '2.0', method: method, params: params || {}, id: id })
    }).then(function (res) {
      return res.json().catch(function () {
        throw new RpcError(CODES.NETWORK, 'The server sent an unexpected answer (HTTP ' + res.status + ').');
      });
    }, function () {
      throw new RpcError(CODES.NETWORK, 'Could not reach the server. Check your internet connection.');
    }).then(function (reply) {
      rpc.lastMeta = reply && reply.meta;
      if (reply && reply.error && reply.error.code === CODES.CSRF_FAILED && window.BotoKo && BotoKo.refreshToken) {
        // Old token (page open too long, or the login changed in another tab): nothing was done on the
        // server. Get a fresh token for this page (same login only), then say so; trying again then works.
        return BotoKo.refreshToken().then(function () {
          throw new RpcError(CODES.CSRF_FAILED, 'This page was open too long or your login changed in another tab. ' +
                                                'Please try again.', reply.error.data, reply.meta);
        });
      }
      if (reply && reply.error) {
        throw new RpcError(reply.error.code, reply.error.message, reply.error.data, reply.meta);
      }
      if (!reply || reply.id !== id) throw new RpcError(CODES.NETWORK, 'The server sent an unexpected answer.');
      return reply.result;
    });
  }

  rpc.codes = CODES;
  rpc.RpcError = RpcError;
  rpc.lastMeta = null;
  window.rpc = rpc;
})();
