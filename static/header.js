(function () {
  'use strict';

  var NAV_ITEMS = [
    { key: 'home', label: 'Home', href: '/' },
    { key: 'status', label: 'Status', href: '/status' },
    { key: 'usage', label: 'Usage', href: '/usage' },
    { key: 'clients', label: 'Clients', href: '/clients' },
    { key: 'mapping', label: 'Mapping', href: '/mapping' },
    { key: 'models', label: 'Models', href: '/models' },
    { key: 'pricing', label: 'Pricing', href: '/pricing' },
    { key: 'throughput', label: 'Throughput', href: '/throughput' },
    { key: 'admin', label: 'Admin', href: '/admin' }
  ];

  function resolveTarget(target) {
    if (!target) return null;
    if (typeof target === 'string') {
      try {
        return document.querySelector(target);
      } catch (err) {
        return null;
      }
    }
    return target.nodeType === 1 ? target : null;
  }

  function resolveActive(active) {
    var i;
    for (i = 0; i < NAV_ITEMS.length; i++) {
      if (NAV_ITEMS[i].key === active) return active;
    }
    var path = (window.location && window.location.pathname) || '/';
    path = path.replace(/\/+$/, '') || '/';
    for (i = 0; i < NAV_ITEMS.length; i++) {
      if (NAV_ITEMS[i].href === path) return NAV_ITEMS[i].key;
    }
    return '';
  }

  function findExisting(root) {
    var children = root.children;
    for (var i = 0; i < children.length; i++) {
      if (children[i].classList && children[i].classList.contains('og-header')) {
        return children[i];
      }
    }
    return null;
  }

  function buildNav(active) {
    var nav = document.createElement('nav');
    nav.className = 'og-nav';
    nav.setAttribute('aria-label', 'Primary');
    for (var i = 0; i < NAV_ITEMS.length; i++) {
      var item = NAV_ITEMS[i];
      var link = document.createElement('a');
      link.href = item.href;
      link.textContent = item.label;
      if (item.key === active) {
        link.className = 'is-active';
        link.setAttribute('aria-current', 'page');
      }
      nav.appendChild(link);
    }
    return nav;
  }

  function mount(target, options) {
    var root = resolveTarget(target);
    if (!root) return null;

    var existing = findExisting(root);
    if (existing) return existing;

    options = options || {};
    var active = resolveActive(options.active);

    var header = document.createElement('header');
    header.className = 'og-header';

    var brand = document.createElement('a');
    brand.className = 'og-brand';
    brand.href = '/';
    brand.textContent = 'OneGate';
    header.appendChild(brand);

    if (typeof options.title === 'string' && options.title.trim()) {
      var title = document.createElement('h1');
      title.className = 'og-title';
      title.textContent = options.title.trim();
      header.appendChild(title);
    }

    header.appendChild(buildNav(active));

    if (typeof options.actions === 'string' && options.actions.trim()) {
      var actions = document.createElement('div');
      actions.className = 'og-actions';
      actions.innerHTML = options.actions;
      header.appendChild(actions);
    }

    while (root.firstChild) root.removeChild(root.firstChild);
    root.appendChild(header);
    return header;
  }

  window.OneGateHeader = {
    mount: mount,
    nav: NAV_ITEMS.slice()
  };
})();