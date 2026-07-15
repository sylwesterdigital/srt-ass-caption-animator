(() => {
  const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const header = document.getElementById('siteHeader');
  const navToggle = document.getElementById('navToggle');
  const siteNav = document.getElementById('siteNav');
  const cursorGlow = document.getElementById('cursorGlow');

  function setHeaderState() {
    header?.classList.toggle('is-scrolled', window.scrollY > 18);
  }

  setHeaderState();
  window.addEventListener('scroll', setHeaderState, { passive: true });

  navToggle?.addEventListener('click', () => {
    const open = navToggle.getAttribute('aria-expanded') !== 'true';
    navToggle.setAttribute('aria-expanded', String(open));
    navToggle.setAttribute('aria-label', open ? 'Close navigation' : 'Open navigation');
    siteNav?.classList.toggle('open', open);
    document.body.classList.toggle('nav-open', open);
  });

  siteNav?.querySelectorAll('a').forEach(link => {
    link.addEventListener('click', () => {
      navToggle?.setAttribute('aria-expanded', 'false');
      navToggle?.setAttribute('aria-label', 'Open navigation');
      siteNav.classList.remove('open');
      document.body.classList.remove('nav-open');
    });
  });

  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && siteNav?.classList.contains('open')) {
      navToggle?.click();
    }
  });

  if (!prefersReducedMotion && cursorGlow) {
    let mouseX = innerWidth * 0.5;
    let mouseY = innerHeight * 0.4;
    let currentX = mouseX;
    let currentY = mouseY;

    window.addEventListener('pointermove', event => {
      mouseX = event.clientX;
      mouseY = event.clientY;
    }, { passive: true });

    const followCursor = () => {
      currentX += (mouseX - currentX) * 0.08;
      currentY += (mouseY - currentY) * 0.08;
      cursorGlow.style.transform = `translate3d(${currentX - 220}px, ${currentY - 220}px, 0)`;
      requestAnimationFrame(followCursor);
    };
    followCursor();
  }

  const reveals = [...document.querySelectorAll('.reveal')];
  reveals.forEach(element => {
    const delay = element.dataset.revealDelay;
    if (delay) element.style.setProperty('--reveal-delay', `${delay}ms`);
  });

  if ('IntersectionObserver' in window && !prefersReducedMotion) {
    const revealObserver = new IntersectionObserver(entries => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-visible');
          revealObserver.unobserve(entry.target);
        }
      });
    }, { threshold: 0.12, rootMargin: '0px 0px -5% 0px' });

    reveals.forEach(element => revealObserver.observe(element));
  } else {
    reveals.forEach(element => element.classList.add('is-visible'));
  }

  const countElements = [...document.querySelectorAll('[data-count]')];
  const animateCount = element => {
    const target = Number(element.dataset.count || 0);
    const duration = 1000;
    const start = performance.now();

    const tick = now => {
      const progress = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - progress, 3);
      element.textContent = String(Math.round(target * eased));
      if (progress < 1) requestAnimationFrame(tick);
    };

    requestAnimationFrame(tick);
  };

  if ('IntersectionObserver' in window) {
    const countObserver = new IntersectionObserver(entries => {
      entries.forEach(entry => {
        if (entry.isIntersecting) {
          animateCount(entry.target);
          countObserver.unobserve(entry.target);
        }
      });
    }, { threshold: 0.65 });

    countElements.forEach(element => countObserver.observe(element));
  } else {
    countElements.forEach(animateCount);
  }

  if (!prefersReducedMotion) {
    const parallaxCards = [...document.querySelectorAll('[data-parallax-strength]')];

    parallaxCards.forEach(card => {
      const strength = Number(card.dataset.parallaxStrength || 10);

      card.addEventListener('pointermove', event => {
        const rect = card.getBoundingClientRect();
        const x = (event.clientX - rect.left) / rect.width - 0.5;
        const y = (event.clientY - rect.top) / rect.height - 0.5;
        card.style.transform = `perspective(1400px) rotateX(${-y * strength}deg) rotateY(${x * strength}deg) translateZ(0)`;
      });

      card.addEventListener('pointerleave', () => {
        card.style.transform = 'perspective(1400px) rotateX(0deg) rotateY(0deg) translateZ(0)';
      });
    });
  }

  const workflowTrack = document.querySelector('.workflow-track');
  const workflowProgress = document.getElementById('workflowProgress');

  function updateWorkflowProgress() {
    if (!workflowTrack || !workflowProgress) return;
    const rect = workflowTrack.getBoundingClientRect();
    const viewportCenter = innerHeight * 0.58;
    const progress = Math.max(0, Math.min(1, (viewportCenter - rect.top) / Math.max(1, rect.height)));
    workflowProgress.style.height = `${progress * 100}%`;
  }

  updateWorkflowProgress();
  window.addEventListener('scroll', updateWorkflowProgress, { passive: true });
  window.addEventListener('resize', updateWorkflowProgress);

  const slides = [...document.querySelectorAll('.showcase-slide')];
  const prevButton = document.getElementById('sliderPrev');
  const nextButton = document.getElementById('sliderNext');
  const currentLabel = document.getElementById('sliderCurrent');
  const totalLabel = document.getElementById('sliderTotal');
  const dotsContainer = document.getElementById('sliderDots');
  let activeSlide = 0;
  let slideTimer = null;

  totalLabel.textContent = String(slides.length).padStart(2, '0');

  slides.forEach((_, index) => {
    const dot = document.createElement('button');
    dot.type = 'button';
    dot.setAttribute('aria-label', `Show slide ${index + 1}`);
    dot.classList.toggle('active', index === 0);
    dot.addEventListener('click', () => {
      goToSlide(index);
      restartSliderTimer();
    });
    dotsContainer.append(dot);
  });

  function goToSlide(index) {
    if (!slides.length || index === activeSlide) return;

    const oldSlide = slides[activeSlide];
    oldSlide.classList.add('leaving');
    oldSlide.classList.remove('active');

    activeSlide = (index + slides.length) % slides.length;
    const newSlide = slides[activeSlide];

    requestAnimationFrame(() => {
      newSlide.classList.add('active');
      oldSlide.classList.remove('leaving');
    });

    currentLabel.textContent = String(activeSlide + 1).padStart(2, '0');
    [...dotsContainer.children].forEach((dot, dotIndex) => {
      dot.classList.toggle('active', dotIndex === activeSlide);
    });
  }

  function restartSliderTimer() {
    clearInterval(slideTimer);
    if (!prefersReducedMotion) {
      slideTimer = setInterval(() => goToSlide(activeSlide + 1), 6500);
    }
  }

  prevButton?.addEventListener('click', () => {
    goToSlide(activeSlide - 1);
    restartSliderTimer();
  });

  nextButton?.addEventListener('click', () => {
    goToSlide(activeSlide + 1);
    restartSliderTimer();
  });

  const slider = document.getElementById('showcaseSlider');
  let touchStartX = 0;

  slider?.addEventListener('touchstart', event => {
    touchStartX = event.changedTouches[0]?.clientX || 0;
  }, { passive: true });

  slider?.addEventListener('touchend', event => {
    const touchEndX = event.changedTouches[0]?.clientX || 0;
    const delta = touchEndX - touchStartX;
    if (Math.abs(delta) > 45) {
      goToSlide(activeSlide + (delta < 0 ? 1 : -1));
      restartSliderTimer();
    }
  }, { passive: true });

  restartSliderTimer();

  document.querySelectorAll('.faq-item button').forEach(button => {
    button.addEventListener('click', () => {
      const item = button.closest('.faq-item');
      const answer = item.querySelector('.faq-answer');
      const isOpen = item.classList.contains('open');

      document.querySelectorAll('.faq-item.open').forEach(openItem => {
        if (openItem !== item) {
          openItem.classList.remove('open');
          openItem.querySelector('button').setAttribute('aria-expanded', 'false');
          openItem.querySelector('.faq-answer').style.maxHeight = '0px';
        }
      });

      item.classList.toggle('open', !isOpen);
      button.setAttribute('aria-expanded', String(!isOpen));
      answer.style.maxHeight = isOpen ? '0px' : `${answer.scrollHeight}px`;
    });
  });

  const canvas = document.getElementById('fluidCanvas');

  if (canvas && !prefersReducedMotion) {
    const ctx = canvas.getContext('2d', { alpha: true });
    let dpr = Math.min(2, window.devicePixelRatio || 1);
    let width = 0;
    let height = 0;
    const blobs = [];
    const palette = [
      [72, 127, 255],
      [91, 217, 255],
      [148, 99, 255],
      [64, 230, 173]
    ];

    function resizeCanvas() {
      dpr = Math.min(2, window.devicePixelRatio || 1);
      width = innerWidth;
      height = innerHeight;
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

      if (!blobs.length) {
        for (let index = 0; index < 7; index += 1) {
          const color = palette[index % palette.length];
          blobs.push({
            x: Math.random() * width,
            y: Math.random() * height,
            radius: Math.min(width, height) * (0.17 + Math.random() * 0.18),
            speedX: (Math.random() - 0.5) * 0.19,
            speedY: (Math.random() - 0.5) * 0.17,
            phase: Math.random() * Math.PI * 2,
            color
          });
        }
      }
    }

    function drawFluid(time) {
      ctx.clearRect(0, 0, width, height);
      ctx.globalCompositeOperation = 'lighter';

      blobs.forEach((blob, index) => {
        blob.x += blob.speedX;
        blob.y += blob.speedY;
        blob.phase += 0.0025 + index * 0.00005;

        const pad = blob.radius;
        if (blob.x < -pad) blob.x = width + pad;
        if (blob.x > width + pad) blob.x = -pad;
        if (blob.y < -pad) blob.y = height + pad;
        if (blob.y > height + pad) blob.y = -pad;

        const pulse = 1 + Math.sin(blob.phase + time * 0.00015) * 0.08;
        const radius = blob.radius * pulse;
        const gradient = ctx.createRadialGradient(
          blob.x,
          blob.y,
          0,
          blob.x,
          blob.y,
          radius
        );

        const [r, g, b] = blob.color;
        gradient.addColorStop(0, `rgba(${r},${g},${b},0.075)`);
        gradient.addColorStop(0.45, `rgba(${r},${g},${b},0.035)`);
        gradient.addColorStop(1, `rgba(${r},${g},${b},0)`);

        ctx.fillStyle = gradient;
        ctx.beginPath();
        ctx.arc(blob.x, blob.y, radius, 0, Math.PI * 2);
        ctx.fill();
      });

      ctx.globalCompositeOperation = 'source-over';
      requestAnimationFrame(drawFluid);
    }

    resizeCanvas();
    addEventListener('resize', resizeCanvas);
    requestAnimationFrame(drawFluid);
  }
})();
