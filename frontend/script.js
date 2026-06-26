/* ===================================================================
   ASTRANAV-LRIS — FRONTEND LOGIC
   Sections: 1) Starfield  2) 3D Moon  3) Scroll reveals
             4) Animated counters  5) Nav behavior
=================================================================== */

/* ---------------------------------------------------------------
   1) AMBIENT 2D STARFIELD (lightweight, behind everything)
---------------------------------------------------------------- */
(function starfield() {
  const canvas = document.getElementById('bg-stars');
  const ctx = canvas.getContext('2d');
  let stars = [];
  let w, h;

  function resize() {
    w = canvas.width = window.innerWidth;
    h = canvas.height = window.innerHeight;
    const count = Math.floor((w * h) / 7000);
    stars = Array.from({ length: count }, () => {
      const depth = Math.random(); // 0 = far/small/slow, 1 = near/large/fast
      const angle = Math.random() * Math.PI * 2;
      const speed = (0.06 + depth * 0.5);
      return {
        x: Math.random() * w,
        y: Math.random() * h,
        r: 0.3 + depth * 1.3,
        baseAlpha: 0.25 + depth * 0.55,
        twinkleSpeed: Math.random() * 0.015 + 0.004,
        phase: Math.random() * Math.PI * 2,
        vx: Math.cos(angle) * speed * 0.25,
        vy: Math.sin(angle) * speed * 0.25 + speed * 0.45, // gentle net downward drift + per-star wander
      };
    });
  }

  let t = 0;
  function draw() {
    t += 1;
    ctx.clearRect(0, 0, w, h);
    for (const s of stars) {
      const alpha = s.baseAlpha + Math.sin(t * s.twinkleSpeed + s.phase) * 0.22;
      ctx.beginPath();
      ctx.fillStyle = `rgba(236,236,234,${Math.max(0, alpha)})`;
      ctx.arc(s.x, s.y, s.r, 0, Math.PI * 2);
      ctx.fill();
      s.x += s.vx;
      s.y += s.vy;
      if (s.y > h + 4) { s.y = -4; s.x = Math.random() * w; }
      if (s.x > w + 4) s.x = -4;
      if (s.x < -4) s.x = w + 4;
    }
    requestAnimationFrame(draw);
  }

  window.addEventListener('resize', resize);
  resize();
  draw();
})();

/* ---------------------------------------------------------------
   2) 3D MOON HERO (Three.js) — procedural lunar surface,
      lit from one side, glowing south-pole marker ring.
---------------------------------------------------------------- */
(function moonScene() {
  const canvasEl = document.getElementById('moon-canvas');
  if (!canvasEl || typeof THREE === 'undefined') return;

  const container = canvasEl.parentElement;
  let width = container.clientWidth;
  let height = container.clientHeight;

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(36, width / height, 0.1, 100);
  camera.position.set(0, 0.1, 6.6);

  const renderer = new THREE.WebGLRenderer({ canvas: canvasEl, antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(width, height);

  /* ---------- Real lunar textures (NASA-derived, public domain) ---------- */
  const loader = new THREE.TextureLoader();
  const albedoTex = loader.load('assets/moon_albedo.jpg');
  const bumpTex = loader.load('assets/moon_bump.jpg');
  albedoTex.anisotropy = 4;

  const moonGroup = new THREE.Group();
  const BASE_TILT_X = -0.92; // brings the south pole (the whole point of the product) into clear view
  moonGroup.rotation.z = 0.18;
  moonGroup.rotation.x = BASE_TILT_X;
  scene.add(moonGroup);

  const geometry = new THREE.SphereGeometry(1.65, 128, 128);
  const material = new THREE.MeshStandardMaterial({
    map: albedoTex,
    bumpMap: bumpTex,
    bumpScale: 0.016,
    roughness: 1,
    metalness: 0,
  });
  const moon = new THREE.Mesh(geometry, material);
  moonGroup.add(moon);

  // Lighting — a single restrained "sun" for a real, soft terminator line,
  // plus a low neutral ambient fill. No colored rim lights, no glow sprites,
  // no emissive overlays — the goal is a plain, documentary-photograph read,
  // not a glossy render.
  const sun = new THREE.DirectionalLight(0xffffff, 2.1);
  sun.position.set(-4, 2.4, 3.2);
  scene.add(sun);
  const fill = new THREE.AmbientLight(0x1a1a1a, 0.55);
  scene.add(fill);

  // subtle mouse parallax
  let targetRotY = 0, targetRotX = 0;
  window.addEventListener('mousemove', (e) => {
    const nx = (e.clientX / window.innerWidth) - 0.5;
    const ny = (e.clientY / window.innerHeight) - 0.5;
    targetRotY = nx * 0.25;
    targetRotX = ny * 0.12;
  });

  function resize() {
    width = container.clientWidth;
    height = container.clientHeight;
    if (!width || !height) return;
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    renderer.setSize(width, height);
  }
  window.addEventListener('resize', resize);

  function animate() {
    requestAnimationFrame(animate);
    moonGroup.rotation.y += 0.0018;
    moonGroup.rotation.x += ((BASE_TILT_X + targetRotX) - moonGroup.rotation.x) * 0.02;
    renderer.render(scene, camera);
  }
  animate();
})();

/* ---------------------------------------------------------------
   3) SCROLL REVEALS — IntersectionObserver, staggered
---------------------------------------------------------------- */
(function scrollReveals() {
  const items = document.querySelectorAll('[data-reveal]');
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry, i) => {
      if (entry.isIntersecting) {
        const delay = (entry.target.dataset.delay) ? Number(entry.target.dataset.delay) : (i % 6) * 80;
        setTimeout(() => entry.target.classList.add('in'), delay);
        observer.unobserve(entry.target);
      }
    });
  }, { threshold: 0.12, rootMargin: '0px 0px -40px 0px' });

  items.forEach(el => observer.observe(el));
})();

/* ---------------------------------------------------------------
   4) ANIMATED NUMBER COUNTERS (hero stats)
---------------------------------------------------------------- */
(function counters() {
  const nums = document.querySelectorAll('[data-count]');
  const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (!entry.isIntersecting) return;
      const el = entry.target;
      const target = parseFloat(el.dataset.count);
      const decimals = Number(el.dataset.decimals || 0);
      const prefix = el.dataset.prefix || '';
      const suffix = el.dataset.suffix || '';
      const duration = 1100;
      const start = performance.now();

      function tick(now) {
        const progress = Math.min(1, (now - start) / duration);
        const eased = 1 - Math.pow(1 - progress, 3);
        const value = target * eased;
        el.textContent = `${prefix}${value.toFixed(decimals)}${suffix}`;
        if (progress < 1) requestAnimationFrame(tick);
      }
      requestAnimationFrame(tick);
      observer.unobserve(el);
    });
  }, { threshold: 0.5 });

  nums.forEach(el => observer.observe(el));
})();

/* ---------------------------------------------------------------
   5) NAV BEHAVIOR — scrolled state + mobile menu
---------------------------------------------------------------- */
(function nav() {
  const navEl = document.getElementById('nav');
  const burger = document.getElementById('navBurger');
  const mobile = document.getElementById('navMobile');

  window.addEventListener('scroll', () => {
    navEl.classList.toggle('scrolled', window.scrollY > 20);
  });

  burger.addEventListener('click', () => {
    const open = mobile.classList.toggle('open');
    burger.setAttribute('aria-expanded', String(open));
  });

  mobile.querySelectorAll('a').forEach(a => a.addEventListener('click', () => {
    mobile.classList.remove('open');
    burger.setAttribute('aria-expanded', 'false');
  }));
})();
