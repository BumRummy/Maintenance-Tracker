(() => {
  const button = document.getElementById('enable-notifications');
  if (!button || !('serviceWorker' in navigator) || !('PushManager' in window)) return;

  const decodeKey = value => {
    const padding = '='.repeat((4 - value.length % 4) % 4);
    const raw = atob((value + padding).replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from([...raw].map(character => character.charCodeAt(0)));
  };

  const showEnabled = () => {
    button.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/></svg>';
    button.setAttribute('aria-label', 'Notifications enabled');
    button.setAttribute('title', 'Notifications enabled');
    button.classList.add('is-enabled');
    button.disabled = true;
  };

  const showDisabled = () => {
    button.textContent = 'Enable notifications';
    button.setAttribute('aria-label', 'Enable notifications');
    button.removeAttribute('title');
    button.classList.remove('is-enabled');
    button.disabled = false;
  };

  const registerSubscription = async () => {
    button.disabled = true;
    try {
      const registration = await navigator.serviceWorker.register('/service-worker.js');
      const publicKey = await loadPublicKey();
      const subscription = await subscribe(registration, publicKey);
      const response = await fetch('/api/push/subscriptions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(subscription)
      });
      if (!response.ok) throw new Error('The notification subscription could not be saved.');
      showEnabled();
    } catch (error) {
      showDisabled();
      window.alert(error.message);
    }
  };

  navigator.serviceWorker.register('/service-worker.js').then(async registration => {
    let subscription = await registration.pushManager.getSubscription();
    if (subscription && Notification.permission === 'granted') {
      showEnabled();
    }
    button.hidden = false;
  }).catch(() => {});

  button.addEventListener('click', registerSubscription);
})();
