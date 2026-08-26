(() => {
  const button = document.getElementById('enable-notifications');
  if (!button || !('serviceWorker' in navigator) || !('PushManager' in window)) return;

  const decodeKey = value => {
    const padding = '='.repeat((4 - value.length % 4) % 4);
    const raw = atob((value + padding).replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from([...raw].map(character => character.charCodeAt(0)));
  };

  const registerSubscription = async () => {
    button.disabled = true;
    try {
      const registration = await navigator.serviceWorker.register('/service-worker.js');
      const configResponse = await fetch('/api/push/config');
      if (!configResponse.ok) throw new Error('Push configuration could not be loaded.');
      const { publicKey } = await configResponse.json();
      if (!publicKey) throw new Error('Notifications are not configured on the server.');

      let subscription = await registration.pushManager.getSubscription();
      if (!subscription) {
        subscription = await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey: decodeKey(publicKey)
        });
      }
      const response = await fetch('/api/push/subscriptions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(subscription)
      });
      if (!response.ok) throw new Error('The notification subscription could not be saved.');
      button.textContent = 'Notifications enabled';
      button.disabled = true;
    } catch (error) {
      button.disabled = false;
      button.textContent = 'Enable notifications';
      window.alert(error.message);
    }
  };

  navigator.serviceWorker.register('/service-worker.js').then(async registration => {
    const subscription = await registration.pushManager.getSubscription();
    if (subscription && Notification.permission === 'granted') {
      button.textContent = 'Notifications enabled';
      button.disabled = true;
    }
    button.hidden = false;
  }).catch(() => {});

  button.addEventListener('click', registerSubscription);
})();
