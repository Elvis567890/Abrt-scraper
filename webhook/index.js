const express = require('express');
const cors = require('cors');
const { createClient } = require('@supabase/supabase-js');

const app = express();
app.use(cors());
app.use(express.json());

// Supabase config
const supabaseUrl = process.env.SUPABASE_URL || 'https://iaruxqgvliyfzpobmbjl.supabase.co';
const supabaseKey = process.env.SUPABASE_SERVICE_ROLE_KEY;
const supabase = createClient(supabaseUrl, supabaseKey);

// Webhook endpoint
app.post('/webhook', async (req, res) => {
  console.log('📨 SMS received:', req.body);

  const { sender, message } = req.body;

  if (!sender || !message) {
    return res.status(400).json({ error: 'Missing sender or message' });
  }

  try {
    // Parse SMS
    const parsed = parseSMS(message);
    if (!parsed) {
      return res.status(400).json({ error: 'Unrecognized SMS format' });
    }

    const { transactionId, amount } = parsed;
    console.log('📊 Parsed:', { transactionId, amount, sender });

    // Check for duplicate transaction
    const { data: existing } = await supabase
      .from('payments')
      .select('id')
      .eq('transaction_id', transactionId)
      .maybeSingle();

    if (existing) {
      return res.status(400).json({ error: 'Duplicate transaction' });
    }

    // Find pending payment
    const { data: pending } = await supabase
      .from('payments')
      .select('*')
      .eq('sender_phone', sender)
      .eq('amount', amount)
      .eq('status', 'pending')
      .order('created_at', { ascending: false })
      .limit(1);

    if (!pending || pending.length === 0) {
      return res.status(404).json({ error: 'No matching pending payment' });
    }

    const payment = pending[0];
    const planConfig = { day_pass: 1, monthly_vip: 30, quarterly_pro: 90 };
    const durationDays = planConfig[payment.selected_plan] || 30;
    const expiry = new Date(Date.now() + durationDays * 24 * 60 * 60 * 1000);

    // Update payment
    await supabase
      .from('payments')
      .update({
        status: 'paid',
        transaction_id: transactionId,
        sms_received_at: new Date().toISOString(),
        verified_at: new Date().toISOString()
      })
      .eq('id', payment.id);

    // Update user subscription
    await supabase
      .from('users')
      .update({
        subscription_status: 'active',
        subscription_plan: payment.selected_plan,
        subscription_start: new Date().toISOString(),
        subscription_expiry: expiry.toISOString()
      })
      .eq('id', payment.user_id);

    console.log('✅ Activated for user:', payment.user_id);
    res.json({ success: true, userId: payment.user_id, transactionId });

  } catch (error) {
    console.error('Error:', error);
    res.status(500).json({ error: 'Internal server error' });
  }
});

app.get('/', (req, res) => {
  res.send('SMS Webhook is running!');
});

function parseSMS(message) {
  const patterns = [
    /received UGX ([\d,]+) from (\d+).*Ref: ([A-Z0-9]+)/i,
    /received ([\d,]+) UGX from (\d+).*Ref: ([A-Z0-9]+)/i,
    /Ref: ([A-Z0-9]+).*received UGX ([\d,]+) from (\d+)/i,
    /Ref: ([A-Z0-9]+).*received ([\d,]+) UGX from (\d+)/i
  ];

  for (const pattern of patterns) {
    const match = message.match(pattern);
    if (match) {
      let amount, senderNumber, transactionId;
      if (pattern.source.includes('Ref: ([A-Z0-9]+).*received')) {
        transactionId = match[1];
        amount = parseFloat(match[2].replace(/,/g, ''));
        senderNumber = match[3];
      } else {
        amount = parseFloat(match[1].replace(/,/g, ''));
        senderNumber = match[2];
        transactionId = match[3];
      }
      return { transactionId, amount, senderNumber };
    }
  }
  return null;
}

const port = process.env.PORT || 3000;
app.listen(port, () => {
  console.log(`🚀 Server running on port ${port}`);
});
