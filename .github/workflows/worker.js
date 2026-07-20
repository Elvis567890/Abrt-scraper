export default {
  async fetch(request, env, ctx) {
    const corsHeaders = {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
    };

    if (request.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders });
    }

    const url = new URL(request.url);
    const path = url.pathname;

    try {
      if (request.method === 'GET' && path.startsWith('/api/get-settings/')) {
        const user_id = path.split('/').pop();
        const result = await env.DB.prepare('SELECT settings FROM user_preferences WHERE user_id = ?').bind(user_id).first();
        const defaultSettings = {
          default_stake: 100000, min_profit: 1.0, max_profit: 15.0, plan: 'free', expiry: null,
          markets: { '1x2': true, 'ou': true, 'ah': true, 'btts': true },
          alert_threshold: 5.0, dark_mode: true, card_layout: 'comfortable'
        };
        const settings = result ? JSON.parse(result.settings) : defaultSettings;
        return Response.json(settings, { headers: corsHeaders });
      }

      if (request.method === 'GET' && path.startsWith('/api/get-subscription/')) {
        const user_id = path.split('/').pop();
        const result = await env.DB.prepare('SELECT settings FROM user_preferences WHERE user_id = ?').bind(user_id).first();
        let plan = 'free', expiry = null;
        if (result) {
          const settings = JSON.parse(result.settings);
          if (settings.expiry && new Date(settings.expiry) > new Date()) {
            plan = settings.plan; expiry = settings.expiry;
          } else if (settings.expiry && new Date(settings.expiry) <= new Date()) {
            settings.plan = 'free'; settings.expiry = null;
            await env.DB.prepare('INSERT OR REPLACE INTO user_preferences (user_id, settings) VALUES (?, ?)').bind(user_id, JSON.stringify(settings)).run();
            plan = 'free'; expiry = null;
          } else {
            plan = settings.plan || 'free'; expiry = settings.expiry || null;
          }
        }
        return Response.json({ plan, expiry }, { headers: corsHeaders });
      }

      if (request.method === 'POST' && path === '/api/save-settings') {
        const body = await request.json();
        const user_id = body.user_id; delete body.user_id;
        const existing = await env.DB.prepare('SELECT settings FROM user_preferences WHERE user_id = ?').bind(user_id).first();
        let existingSettings = existing ? JSON.parse(existing.settings) : {};
        body.plan = existingSettings.plan || 'free';
        body.expiry = existingSettings.expiry || null;
        await env.DB.prepare('INSERT OR REPLACE INTO user_preferences (user_id, settings) VALUES (?, ?)').bind(user_id, JSON.stringify(body)).run();
        return Response.json({ status: 'success' }, { headers: corsHeaders });
      }

      if (request.method === 'POST' && path === '/api/webhook') {
        const payload = await request.json();
        const event = payload['event.type'] || payload['event'];
        if (event === 'charge.completed') {
          const user_id = payload.meta?.user_id || payload.meta?.userId; 
          const amount = payload.amount;
          let expiry = new Date(); let plan_type = 'free';
          if (amount >= 40000) { expiry.setDate(expiry.getDate() + 90); plan_type = 'quarterly'; }
          else if (amount >= 15000) { expiry.setDate(expiry.getDate() + 30); plan_type = 'monthly'; }
          else if (amount >= 2500) { expiry.setDate(expiry.getDate() + 1); plan_type = 'daily'; }
          if (user_id) {
            const current = await env.DB.prepare('SELECT settings FROM user_preferences WHERE user_id = ?').bind(user_id).first();
            let settings = current ? JSON.parse(current.settings) : {};
            settings.plan = plan_type; settings.expiry = expiry.toISOString();
            await env.DB.prepare('INSERT OR REPLACE INTO user_preferences (user_id, settings) VALUES (?, ?)').bind(user_id, JSON.stringify(settings)).run();
          }
        }
        return Response.json({ status: 'webhook_received' }, { headers: corsHeaders });
      }

      return new Response('Not Found', { status: 404, headers: corsHeaders });
    } catch (error) {
      return Response.json({ error: error.message }, { status: 500, headers: corsHeaders });
    }
  }
};
