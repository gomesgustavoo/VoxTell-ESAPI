/* ============================================================================
   VoxTell landing — shared page behaviour. No dependencies, no build step.

   Ported from dicomsegvr/landing/main.js, deliberately MINUS two blocks:
     - the magnetic-CTA pointer effect. It is the one accessory to remove: this
       page is about writing structures into a patient's plan, and cursor-chasing
       buttons undercut that. It was also the only effect not tied to content.
     - the hero-tilt block, which was already dead code there (no .stack element).

   VERSIONED FILENAME: /assets/ is immutable-1y and Cloudflare's Free plan adds a
   4-hour edge Browser TTL that no origin header overrides, so a new URL is the
   only reliable cache bust. Bump the integer here and in every page linking it.
   ========================================================================= */

(function () {
  "use strict";

  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var forEach = function (list, fn) { Array.prototype.forEach.call(list, fn); };

  /* -- 1. reveal on scroll ------------------------------------------------ */

  var revealables = document.querySelectorAll(".reveal");

  if (!("IntersectionObserver" in window) || reduceMotion) {
    forEach(revealables, function (el) { el.classList.add("is-visible"); });
  } else {
    var revealObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        entry.target.classList.add("is-visible");
        revealObserver.unobserve(entry.target);
      });
    }, { threshold: 0.12, rootMargin: "0px 0px -6% 0px" });
    forEach(revealables, function (el) { revealObserver.observe(el); });
  }

  /* -- 2. mobile nav ----------------------------------------------------- */

  var toggle = document.getElementById("navToggle");
  var menu = document.getElementById("navMenu");

  if (toggle && menu) {
    var setOpen = function (open) {
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      toggle.setAttribute("aria-label", open ? "Close menu" : "Open menu");
      menu.classList.toggle("is-open", open);
      document.documentElement.classList.toggle("nav-open", open);
    };

    toggle.addEventListener("click", function () {
      setOpen(toggle.getAttribute("aria-expanded") !== "true");
    });
    menu.addEventListener("click", function (e) {
      if (e.target.closest("a")) setOpen(false);
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape") setOpen(false);
    });
    document.addEventListener("click", function (e) {
      if (!e.target.closest(".nav")) setOpen(false);
    });
  }

  /* -- 3. FAQ: keep one open at a time ----------------------------------- */

  var faqItems = document.querySelectorAll(".faq__item");
  forEach(faqItems, function (item) {
    item.addEventListener("toggle", function () {
      if (!item.open) return;
      forEach(faqItems, function (other) {
        if (other !== item) other.removeAttribute("open");
      });
    });
  });

  /* -- 4. nav hairline on scroll ----------------------------------------- */

  var nav = document.querySelector(".nav");
  if (nav) {
    var syncNav = function () { nav.classList.toggle("is-scrolled", window.scrollY > 8); };
    syncNav();
    window.addEventListener("scroll", syncNav, { passive: true });
  }

  /* -- 5. count-up on the provenance figures -----------------------------
     Values carry data-count (the target) plus optional data-prefix/suffix/
     decimals. Reduced motion snaps to the final value rather than skipping it. */

  var counters = document.querySelectorAll("[data-count]");

  var render = function (el, value) {
    var decimals = parseInt(el.getAttribute("data-decimals") || "0", 10);
    var text = value.toLocaleString("en-US", {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals
    });
    el.textContent = (el.getAttribute("data-prefix") || "") + text +
                     (el.getAttribute("data-suffix") || "");
  };

  var runCount = function (el) {
    var target = parseFloat(el.getAttribute("data-count"));
    if (isNaN(target)) return;
    if (reduceMotion) { render(el, target); return; }
    var start = null;
    var step = function (now) {
      if (start === null) start = now;
      var p = Math.min(1, (now - start) / 1100);
      render(el, target * (1 - Math.pow(1 - p, 3)));
      if (p < 1) window.requestAnimationFrame(step);
    };
    window.requestAnimationFrame(step);
  };

  if (!("IntersectionObserver" in window)) {
    forEach(counters, function (el) { render(el, parseFloat(el.getAttribute("data-count"))); });
  } else {
    var countObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) return;
        runCount(entry.target);
        countObserver.unobserve(entry.target);
      });
    }, { threshold: 0.6 });
    forEach(counters, function (el) { countObserver.observe(el); });
  }
})();
