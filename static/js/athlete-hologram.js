/* ══════════════════════════════════════════════════════════════════════
   AthleteHologram — interactive 3D athlete hologram for the dashboard.

   This project has no bundler and no React, so the component is exposed the
   way the rest of the app works: a global factory that mounts into an
   element and returns a handle.

       var holo = AthleteHologram.mount(el, {
         autoRotate: true, scale: 1, animationSpeed: 1, className: ''
       });
       holo.dispose();

   Everything is optional and every failure path is handled: if WebGL is
   missing, the CDN is blocked, or the model 404s, `mount` leaves whatever
   fallback markup is already inside the element untouched and resolves
   quietly. The dashboard must never go down with the hologram.

   ── Asset note ───────────────────────────────────────────────────────
   static/models/athlete-hologram.glb is a STATIC mesh: 30,875 triangles,
   POSITION + NORMAL only, no textures, no skeleton, no animation clips
   (20.5 MB of scan reduced to 878 KB, since the hologram replaces every
   material anyway). The figure holds a slow turntable and a breathing
   idle.

   The loader still plays `gltf.animations` through an AnimationMixer if it
   finds any, so dropping in a rigged, animated GLB later starts the motion
   with no code change.
   ══════════════════════════════════════════════════════════════════════ */
(function (global) {
  'use strict';

  // jsDelivr, not cdnjs: cdnjs publishes only three's core build, so its
  // examples/js/loaders/GLTFLoader.js path 404s. Both files are allowed by
  // the app's CSP (script-src includes cdn.jsdelivr.net).
  var THREE_SRC = 'https://cdn.jsdelivr.net/npm/three@0.128.0/build/three.min.js';
  var GLTF_SRC = 'https://cdn.jsdelivr.net/npm/three@0.128.0/examples/js/loaders/GLTFLoader.js';
  var MODEL_URL = '/static/models/athlete-hologram.glb';

  /* AthletixAI palette */
  var C = {
    deep: 0x061520,      // background navy
    core: 0x00b8d9,      // supporting cyan
    edge: 0x00e5ff,      // primary neon cyan
    hot: 0xb9fbff,       // rim highlight
    teal: 0x34d399       // accent
  };

  var loaded = {};
  function loadScript(src) {
    if (loaded[src]) return loaded[src];
    loaded[src] = new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src = src;
      s.async = true;
      s.onload = resolve;
      s.onerror = function () { loaded[src] = null; reject(new Error('blocked: ' + src)); };
      document.head.appendChild(s);
    });
    return loaded[src];
  }

  function libs() {
    return loadScript(THREE_SRC).then(function () {
      if (!global.THREE) throw new Error('three.js did not initialise');
      return loadScript(GLTF_SRC);
    }).then(function () {
      if (!global.THREE.GLTFLoader) throw new Error('GLTFLoader missing');
      return global.THREE;
    });
  }

  function webglOK() {
    try {
      var c = document.createElement('canvas');
      return !!(global.WebGLRenderingContext &&
        (c.getContext('webgl') || c.getContext('experimental-webgl')));
    } catch (e) { return false; }
  }

  function reducedMotion() {
    return !!(global.matchMedia &&
      global.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  /* ── the hologram surface ───────────────────────────────────────────
     A Fresnel rim (bright at grazing angles, near-transparent head-on),
     travelling scanlines, and a faint vertical sweep. Lighting is baked
     into the shader so the look never depends on scene lights. */
  function holoMaterial(THREE) {
    return new THREE.ShaderMaterial({
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
      side: THREE.FrontSide,
      uniforms: {
        uTime: { value: 0 },
        uCore: { value: new THREE.Color(C.core) },
        uEdge: { value: new THREE.Color(C.edge) },
        uHot: { value: new THREE.Color(C.hot) },
        uOpacity: { value: 1.0 },
        uScan: { value: 1.0 }
      },
      vertexShader: [
        'varying vec3 vNormalW;',
        'varying vec3 vViewDir;',
        'varying vec3 vPosL;',
        'void main(){',
        '  vPosL = position;',
        '  vec4 world = modelMatrix * vec4(position, 1.0);',
        '  vNormalW = normalize(mat3(modelMatrix) * normal);',
        '  vViewDir = normalize(cameraPosition - world.xyz);',
        '  gl_Position = projectionMatrix * viewMatrix * world;',
        '}'
      ].join('\n'),
      fragmentShader: [
        'uniform float uTime; uniform float uOpacity; uniform float uScan;',
        'uniform vec3 uCore; uniform vec3 uEdge; uniform vec3 uHot;',
        'varying vec3 vNormalW; varying vec3 vViewDir; varying vec3 vPosL;',
        'void main(){',
        '  float f = 1.0 - clamp(dot(normalize(vNormalW), normalize(vViewDir)), 0.0, 1.0);',
        '  float rim = pow(f, 2.4);',              // Fresnel edge glow
        '  float core = pow(f, 0.9) * 0.34;',      // soft body fill
        '  float scan = sin(vPosL.y * 78.0 - uTime * 2.6) * 0.5 + 0.5;',
        '  scan = smoothstep(0.55, 1.0, scan) * 0.20 * uScan;',
        '  float sweep = smoothstep(0.03, 0.0, abs(fract(vPosL.y * 0.32 - uTime * 0.11) - 0.5));',
        '  vec3 col = mix(uCore, uEdge, rim);',
        '  col = mix(col, uHot, rim * rim * 0.85 + sweep * 0.5);',
        '  float a = (core + rim * 0.95 + scan + sweep * 0.30) * uOpacity;',
        '  if (a < 0.012) discard;',
        '  gl_FragColor = vec4(col, clamp(a, 0.0, 1.0));',
        '}'
      ].join('\n')
    });
  }

  function buildPlatform(THREE, group, radius) {
    var rings = [[1.00, 0.55], [0.74, 0.38], [0.50, 0.26], [0.30, 0.18]];
    rings.forEach(function (r, i) {
      var rad = radius * r[0];
      var ring = new THREE.Mesh(
        new THREE.RingGeometry(rad, rad * 1.035, 96),
        new THREE.MeshBasicMaterial({
          color: i === 0 ? C.edge : C.core, transparent: true,
          opacity: r[1], side: THREE.DoubleSide, depthWrite: false,
          blending: THREE.AdditiveBlending
        })
      );
      ring.rotation.x = -Math.PI / 2;
      ring.userData.pulse = i;
      group.add(ring);
    });

    /* circular HUD ticks */
    var ticks = new THREE.Group();
    for (var a = 0; a < 48; a++) {
      var ang = (a / 48) * Math.PI * 2;
      var long = a % 4 === 0;
      var inner = radius * (long ? 1.05 : 1.08);
      var outer = radius * (long ? 1.17 : 1.12);
      var geo = new THREE.BufferGeometry().setFromPoints([
        new THREE.Vector3(Math.cos(ang) * inner, 0, Math.sin(ang) * inner),
        new THREE.Vector3(Math.cos(ang) * outer, 0, Math.sin(ang) * outer)
      ]);
      ticks.add(new THREE.Line(geo, new THREE.LineBasicMaterial({
        color: C.core, transparent: true, opacity: long ? 0.5 : 0.22,
        blending: THREE.AdditiveBlending
      })));
    }
    group.add(ticks);

    /* soft disc of light rising off the floor */
    var disc = new THREE.Mesh(
      new THREE.CircleGeometry(radius * 1.04, 64),
      new THREE.MeshBasicMaterial({
        color: C.core, transparent: true, opacity: 0.10,
        side: THREE.DoubleSide, depthWrite: false,
        blending: THREE.AdditiveBlending
      })
    );
    disc.rotation.x = -Math.PI / 2;
    disc.position.y = 0.002;
    group.add(disc);
    return ticks;
  }

  function buildParticles(THREE, radius, height, count) {
    var pos = new Float32Array(count * 3);
    var seed = new Float32Array(count);
    for (var i = 0; i < count; i++) {
      var ang = Math.random() * Math.PI * 2;
      var rad = radius * (0.35 + Math.random() * 0.95);
      pos[i * 3] = Math.cos(ang) * rad;
      pos[i * 3 + 1] = Math.random() * height;
      pos[i * 3 + 2] = Math.sin(ang) * rad;
      seed[i] = Math.random();
    }
    var geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    var pts = new THREE.Points(geo, new THREE.PointsMaterial({
      color: C.edge, size: 0.019, transparent: true, opacity: 0.75,
      depthWrite: false, blending: THREE.AdditiveBlending, sizeAttenuation: true
    }));
    pts.userData.seed = seed;
    pts.userData.height = height;
    return pts;
  }

  /* ── public API ─────────────────────────────────────────────────── */
  function mount(host, opts) {
    opts = opts || {};
    var autoRotate = opts.autoRotate !== false;
    var userScale = typeof opts.scale === 'number' ? opts.scale : 1;
    var animationSpeed = typeof opts.animationSpeed === 'number' ? opts.animationSpeed : 1;
    var modelUrl = opts.modelUrl || MODEL_URL;
    var onReady = opts.onReady || function () {};
    var onError = opts.onError || function () {};

    var handle = { dispose: function () {}, ok: false };
    if (!host) return handle;
    if (opts.className) host.classList.add(opts.className);

    if (!webglOK()) {
      host.classList.add('holo-no-webgl');
      onError(new Error('WebGL unavailable'));
      return handle;                       // existing fallback markup stays
    }

    var slow = reducedMotion();
    host.classList.add('holo-loading');

    var cancelled = false;
    handle.dispose = function () { cancelled = true; };

    libs().then(function (THREE) {
      if (cancelled || !document.body.contains(host)) return;
      return new Promise(function (resolve, reject) {
        new THREE.GLTFLoader().load(modelUrl, resolve, undefined, function () {
          reject(new Error('model failed to load: ' + modelUrl));
        });
      }).then(function (gltf) { return { THREE: THREE, gltf: gltf }; });
    }).then(function (ctx) {
      if (!ctx || cancelled || !document.body.contains(host)) return;
      var THREE = ctx.THREE, gltf = ctx.gltf;

      var w = host.clientWidth || 320;
      var h = host.clientHeight || 400;

      var renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true });
      renderer.setSize(w, h);
      // Cap the pixel ratio: a 3x phone screen would otherwise render 9x the
      // pixels for no visible gain on a 300px canvas.
      renderer.setPixelRatio(Math.min(global.devicePixelRatio || 1, 2));
      renderer.domElement.className = 'holo-canvas';

      var scene = new THREE.Scene();
      var camera = new THREE.PerspectiveCamera(30, w / h, 0.1, 100);

      var root = new THREE.Group();
      var model = gltf.scene;

      /* Normalise whatever the artist exported: centre it, stand it on the
         floor, and scale it to a known height so `scale` is a predictable
         multiplier rather than a guess about the source units. */
      var box = new THREE.Box3().setFromObject(model);
      var size = box.getSize(new THREE.Vector3());
      var centre = box.getCenter(new THREE.Vector3());
      var targetH = 2.0;
      var fit = size.y > 0.0001 ? targetH / size.y : 1;
      model.scale.setScalar(fit * userScale);
      model.position.set(-centre.x * fit * userScale,
                         -box.min.y * fit * userScale,
                         -centre.z * fit * userScale);

      var mats = [];
      var overlays = [];
      model.traverse(function (o) {
        if (!o.isMesh) return;
        o.castShadow = o.receiveShadow = false;
        if (o.material) {
          (Array.isArray(o.material) ? o.material : [o.material])
            .forEach(function (m) { m.dispose && m.dispose(); });
        }
        var holo = holoMaterial(THREE);
        o.material = holo;
        mats.push(holo);

        /* faint wireframe so the surface reads as a digital scan without
           drowning the silhouette */
        var wire = new THREE.Mesh(o.geometry, new THREE.MeshBasicMaterial({
          color: C.edge, wireframe: true, transparent: true, opacity: 0.055,
          depthWrite: false, blending: THREE.AdditiveBlending
        }));
        wire.scale.setScalar(1.001);
        overlays.push({ mesh: wire, from: o });
      });
      overlays.forEach(function (o) { o.from.add(o.mesh); });

      root.add(model);

      var radius = Math.max(size.x, size.z) * fit * userScale * 0.92 || 0.9;
      radius = Math.max(0.7, Math.min(radius, 1.25));
      var ticks = buildPlatform(THREE, root, radius);
      var particles = buildParticles(THREE, radius * 1.15, targetH * userScale, slow ? 26 : 70);
      root.add(particles);

      /* cyan light rising from the platform - subtle, the shader does the work */
      var up = new THREE.PointLight(C.edge, 1.15, 6);
      up.position.set(0, 0.22, 0.35);
      root.add(up);
      scene.add(root);

      /* frame the figure */
      camera.position.set(0, targetH * 0.62, 6.0);
      camera.lookAt(0, targetH * 0.46, 0);

      /* Skeletal animation, if the GLB carries any. A rigged export drops in
         here with no code change; this model has none (see the asset note). */
      var mixer = null;
      var clips = gltf.animations || [];
      if (clips.length) {
        mixer = new THREE.AnimationMixer(model);
        var action = mixer.clipAction(clips[0]);
        action.setLoop(THREE.LoopRepeat, Infinity);
        action.clampWhenFinished = false;
        action.timeScale = animationSpeed;
        action.play();
      }

      host.classList.remove('holo-loading');
      host.classList.add('holo-live');
      host.insertBefore(renderer.domElement, host.firstChild);

      var clock = new THREE.Clock();
      var raf = 0;
      var t = 0;

      function frame() {
        raf = requestAnimationFrame(frame);
        if (document.hidden) return;              // no work in a background tab
        var dt = Math.min(clock.getDelta(), 0.05);
        t += dt;

        if (mixer) mixer.update(dt * animationSpeed);

        for (var i = 0; i < mats.length; i++) mats[i].uniforms.uTime.value = t;

        if (!slow) {
          if (autoRotate) root.rotation.y = Math.sin(t * 0.22) * 0.62;
          // breathing idle - deliberately small, this is not the FBX clip
          if (!mixer) model.position.y += Math.sin(t * 1.25) * 0.00035;
          ticks.rotation.y = t * 0.06;

          var p = particles.geometry.attributes.position;
          var seed = particles.userData.seed;
          for (var k = 0; k < seed.length; k++) {
            p.array[k * 3 + 1] += (0.10 + seed[k] * 0.16) * dt;
            if (p.array[k * 3 + 1] > particles.userData.height) p.array[k * 3 + 1] = 0;
          }
          p.needsUpdate = true;

          root.children.forEach(function (c) {
            if (c.userData && typeof c.userData.pulse === 'number' && c.material) {
              c.material.opacity = 0.20 + 0.34 *
                (0.5 + 0.5 * Math.sin(t * 1.1 - c.userData.pulse * 0.8));
            }
          });
        }
        renderer.render(scene, camera);
      }
      frame();

      function onResize() {
        if (!document.body.contains(host)) return;
        var nw = host.clientWidth || w, nh = host.clientHeight || h;
        camera.aspect = nw / nh;
        camera.updateProjectionMatrix();
        renderer.setSize(nw, nh);
      }
      global.addEventListener('resize', onResize);

      handle.ok = true;
      handle.dispose = function () {
        cancelled = true;
        cancelAnimationFrame(raf);
        global.removeEventListener('resize', onResize);
        if (mixer) mixer.stopAllAction();
        scene.traverse(function (o) {
          if (o.geometry) o.geometry.dispose();
          if (o.material) {
            (Array.isArray(o.material) ? o.material : [o.material])
              .forEach(function (m) { m.dispose && m.dispose(); });
          }
        });
        renderer.dispose();
        if (renderer.domElement.parentNode) {
          renderer.domElement.parentNode.removeChild(renderer.domElement);
        }
        host.classList.remove('holo-live');
      };
      handle.hasAnimation = clips.length > 0;
      onReady(handle);
    }).catch(function (err) {
      host.classList.remove('holo-loading');
      host.classList.add('holo-failed');
      // The SVG hologram already in the element remains visible.
      if (global.console) console.warn('AthleteHologram:', err.message);
      onError(err);
    });

    return handle;
  }

  global.AthleteHologram = { mount: mount, MODEL_URL: MODEL_URL };
})(window);
