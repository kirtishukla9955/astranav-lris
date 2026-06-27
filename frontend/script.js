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
   2) 3D MOON HERO (Three.js) — disabled in favor of isometric visual
---------------------------------------------------------------- */
// 3D moon canvas removed to match redesigned isometric telemetry panel.

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
