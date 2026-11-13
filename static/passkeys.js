// Passkeys (accounts/passkeys.py): the browser's WebAuthn API, with the JSON the portal gives and takes.
// A button with data-passkey="register" (profile) or data-passkey="signin" (the second step of the sign-in).
(function () {
  "use strict";
  var b64u = {
    toBuf: function (s) {
      s = s.replace(/-/g, "+").replace(/_/g, "/");
      while (s.length % 4) s += "=";
      var bin = atob(s), out = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
      return out.buffer;
    },
    fromBuf: function (buf) {
      var bytes = new Uint8Array(buf), bin = "";
      for (var i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
      return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    }
  };
  function csrf() {
    var el = document.querySelector("input[name=csrfmiddlewaretoken]");
    return el ? el.value : "";
  }
  function post(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().then(function (j) { if (!r.ok) { var e = new Error(j.error || r.status); e.next = j.next; throw e; } return j; });
    });
  }
  function creds(list) {
    return (list || []).map(function (c) { return Object.assign({}, c, { id: b64u.toBuf(c.id) }); });
  }
  function show(btn, msg) {
    var out = document.getElementById(btn.getAttribute("data-out"));
    if (out) { out.textContent = msg; out.hidden = !msg; }
  }
  function register(btn) {
    var name = (document.getElementById(btn.getAttribute("data-name")) || {}).value || "";
    return post(btn.getAttribute("data-options")).then(function (o) {
      o.challenge = b64u.toBuf(o.challenge);
      o.user.id = b64u.toBuf(o.user.id);
      o.excludeCredentials = creds(o.excludeCredentials);
      return navigator.credentials.create({ publicKey: o });
    }).then(function (c) {
      return post(btn.getAttribute("data-verify"), {
        name: name,
        credential: {
          id: c.id, rawId: b64u.fromBuf(c.rawId), type: c.type,
          clientExtensionResults: c.getClientExtensionResults ? c.getClientExtensionResults() : {},
          authenticatorAttachment: c.authenticatorAttachment || null,
          response: {
            clientDataJSON: b64u.fromBuf(c.response.clientDataJSON),
            attestationObject: b64u.fromBuf(c.response.attestationObject),
            transports: c.response.getTransports ? c.response.getTransports() : []
          }
        }
      });
    });
  }
  function signin(btn) {
    return post(btn.getAttribute("data-options")).then(function (o) {
      o.challenge = b64u.toBuf(o.challenge);
      o.allowCredentials = creds(o.allowCredentials);
      return navigator.credentials.get({ publicKey: o });
    }).then(function (c) {
      return post(btn.getAttribute("data-verify"), {
        credential: {
          id: c.id, rawId: b64u.fromBuf(c.rawId), type: c.type,
          clientExtensionResults: c.getClientExtensionResults ? c.getClientExtensionResults() : {},
          authenticatorAttachment: c.authenticatorAttachment || null,
          response: {
            clientDataJSON: b64u.fromBuf(c.response.clientDataJSON),
            authenticatorData: b64u.fromBuf(c.response.authenticatorData),
            signature: b64u.fromBuf(c.response.signature),
            userHandle: c.response.userHandle ? b64u.fromBuf(c.response.userHandle) : null
          }
        }
      });
    });
  }
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest("[data-passkey]");
    if (!btn) return;
    ev.preventDefault();
    if (!window.PublicKeyCredential) { show(btn, btn.getAttribute("data-unsupported")); return; }
    btn.disabled = true; show(btn, "");
    (btn.getAttribute("data-passkey") === "register" ? register(btn) : signin(btn)).then(function (r) {
      window.location.href = r.next || window.location.href;
    }).catch(function (e) {
      btn.disabled = false;
      if (e && e.next) { window.location.href = e.next; return; }
      show(btn, e && e.name === "NotAllowedError" ? btn.getAttribute("data-cancelled") : (e && e.message) || "");
    });
  });
})();
