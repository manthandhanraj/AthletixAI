/* ══════════════════════════════════════════════════════════════════════
   Auth screens — athlete/coach x login/create.

   Four designs, one markup tree. Role and mode are already state in this
   app (authSetRole / authSetTab), so this module reacts to that state and
   swaps the hero copy, hero art and the coach-only field, instead of
   duplicating four pages.

   It hooks the existing functions rather than replacing them, so every
   original behaviour — validation, CSRF, the forgot-password flow, the
   sport/age fields the backend needs — keeps working untouched.

   Direct links (each reference screen is addressable):
     #/login/athlete   #/login/coach   #/signup/athlete   #/signup/coach

   Hero art: the card is a CSS layer. To use a real photograph set
   --hero-img on #authHeroMedia; the placeholder silhouette disappears on
   its own. Per-screen images can be put in HERO[...].img below.
   ══════════════════════════════════════════════════════════════════════ */
(function (global) {
  'use strict';

  var byId = function (id) { return document.getElementById(id); };

  /* Copy per screen. English is the source language — the app's translator
     walks these text nodes after render, so they follow the language
     picker like everything else. */
  var HERO = {
    'athlete:login': {
      title: 'Own your', accent: 'performance.',
      sub: 'AI insights for India’s next generation athletes.',
      keys: 'ANALYSE · IMPROVE · ACHIEVE',
      img: "url('/static/img/hero-athlete.webp')"
    },
    'coach:login': {
      title: 'Lead every', accent: 'athlete forward.',
      sub: 'AI insights for better coaching decisions.',
      keys: 'ANALYSE · GUIDE · ACHIEVE',
      img: "url('/static/img/hero-coach.webp')"
    },
    'athlete:signup': {
      title: 'Start your', accent: 'performance journey.',
      sub: 'Personalised AI insights built around you.',
      keys: 'ANALYSE · IMPROVE · ACHIEVE',
      img: "url('/static/img/hero-athlete.webp')"
    },
    'coach:signup': {
      title: 'Build champions,', accent: 'one insight at a time.',
      sub: 'AI-powered tools for smarter coaching.',
      keys: 'ANALYSE · GUIDE · ACHIEVE',
      img: "url('/static/img/hero-coach.webp')"
    }
  };

  function currentRole() {
    var a = byId('roleAthlete');
    return (a && a.classList.contains('sel-athlete')) ? 'athlete' : 'coach';
  }
  function currentMode() {
    var t = byId('tabSignup');
    return (t && t.classList.contains('on')) ? 'signup' : 'login';
  }

  /* Paint whichever of the four screens the current state describes. */
  function paint() {
    var role = currentRole(), mode = currentMode();
    var h = HERO[role + ':' + mode] || HERO['athlete:login'];

    var title = byId('authHeroTitle');
    if (title) {
      // two nodes so the translator can match each line on its own
      title.textContent = h.title + ' ';
      var span = document.createElement('span');
      span.textContent = h.accent;
      title.appendChild(span);
    }
    var sub = byId('authHeroSub');
    if (sub) sub.textContent = h.sub;
    var keys = byId('authHeroKeys');
    if (keys) keys.textContent = h.keys;

    var media = byId('authHeroMedia');
    if (media) {
      media.style.setProperty('--hero-img', h.img || 'none');
      media.classList.toggle('has-photo', !!h.img);
    }

    /* coaching organisation is a coach-only field */
    var coachExtra = byId('coachExtra');
    if (coachExtra) coachExtra.style.display = (role === 'coach') ? 'block' : 'none';

    /* keep aria in step with the visual selection */
    var a = byId('roleAthlete'), c = byId('roleCoach');
    if (a) a.setAttribute('aria-pressed', String(role === 'athlete'));
    if (c) c.setAttribute('aria-pressed', String(role === 'coach'));

    var hash = '#/' + mode + '/' + role;
    if (global.location.hash !== hash) {
      try { history.replaceState(null, '', hash); } catch (e) {}
    }
    try { if (typeof applyLang === 'function') applyLang(); } catch (e) {}
  }

  /* Wrap the app's own state changers so the screen repaints after them,
     without altering what they do. */
  function wrap(name) {
    var original = global[name];
    if (typeof original !== 'function' || original.__axWrapped) return;
    var wrapped = function () {
      var out = original.apply(this, arguments);
      paint();
      return out;
    };
    wrapped.__axWrapped = true;
    global[name] = wrapped;
  }

  /* Read a screen out of the URL: #/signup/coach */
  function applyHash() {
    var m = /^#\/(login|signup)\/(athlete|coach)$/.exec(global.location.hash || '');
    if (!m) return false;
    if (typeof global.authSetRole === 'function') global.authSetRole(m[2]);
    if (typeof global.authSetTab === 'function') global.authSetTab(m[1]);
    return true;
  }

  function init() {
    if (!byId('authScreen')) return;
    wrap('authSetRole');
    wrap('authSetTab');
    wrap('openForgot');
    wrap('closeForgot');

    /* password show/hide */
    document.addEventListener('click', function (e) {
      var btn = e.target.closest && e.target.closest('.ax-eye');
      if (!btn) return;
      var input = byId(btn.getAttribute('data-eye'));
      if (!input) return;
      var show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      btn.setAttribute('aria-pressed', String(show));
      btn.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
      input.focus();
    });

    /* Google / Phone are not wired to a provider yet. Say so plainly
       rather than presenting a button that silently does nothing. */
    document.addEventListener('click', function (e) {
      var btn = e.target.closest && e.target.closest('.ax-social-btn');
      if (!btn) return;
      var msg = byId('authMsg');
      if (msg) {
        msg.style.color = '';
        msg.textContent = (btn.getAttribute('data-social') === 'google'
          ? 'Google sign-in' : 'Phone sign-in') +
          ' is not connected yet. Please use your email and password.';
      }
    });

    /* keyboard parity for the role cards and the span-based links */
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' && e.key !== ' ') return;
      var el = e.target;
      if (!el || !el.classList) return;
      if (el.classList.contains('ax-link') || el.classList.contains('auth-tab')) {
        e.preventDefault();
        el.click();
      }
    });

    if (!applyHash()) paint();
    global.addEventListener('hashchange', function () { applyHash(); });
  }

  /* This file is deferred, so it runs while readyState is "interactive" —
     before DOMContentLoaded. The app registers its own DOMContentLoaded
     handler during parsing, and that handler resets the role to athlete.
     Queueing behind it (rather than running now) is what lets a URL like
     #/signup/coach survive. */
  if (document.readyState === 'complete') {
    init();
  } else {
    document.addEventListener('DOMContentLoaded', init);
  }

  global.AuthScreens = { paint: paint, HERO: HERO };
})(window);
