const express = require('express');
const cors = require('cors');
const admin = require('firebase-admin');

const app = express();
app.use(cors());
app.use(express.json());

// Use your variable names (without FIREBASE_ prefix)
const serviceAccount = {
  type: process.env.type,
  project_id: process.env.project_id,
  private_key_id: process.env.private_key_id,
  private_key: process.env.private_key,
  client_email: process.env.client_email,
  client_id: process.env.client_id,
  auth_uri: process.env.auth_uri,
  token_uri: process.env.token_uri,
  auth_provider_x509_cert_url: process.env.auth_provider_x509_cert_url,
  client_x509_cert_url: process.env.client_x509_cert_url,
  universe_domain: process.env.universe_domain
};

// Initialize Firebase
admin.initializeApp({
  credential: admin.credential.cert(serviceAccount)
});

const db = admin.firestore();

app.post('/webhook', async (req, res) => {
  console.log('📨 SMS received:', req.body);

  const sender = req.body.sender || req.body.from;
  const message = req.body.message || req.body.text;

  if (!sender || !message) {
    return res.status(400).json({ error: 'Missing sender or message' });
  }

  try {
    const parsed = parseSMS(message);
    if (!parsed) {
      return res.status(400).json({ error: 'Unrecognized SMS format' });
    }

    const { transactionId, amount } = parsed;

    const duplicateSnapshot = await db.collection('payments')
      .where('transactionId', '==', transactionId)
      .get();

    if (!duplicateSnapshot.empty) {
      return res.status(400).json({ error: 'Duplicate transaction' });
    }

    const pendingSnapshot = await db.collection('payments')
      .where('senderPhone', '==', sender)
      .where('amount', '==', amount)
      .where('status', '==', 'pending')
      .orderBy('createdAt', 'desc')
      .limit(1)
      .get();

    if (pendingSnapshot.empty) {
      return res.status(404).json({ error: 'No matching pending payment' });
    }

    const paymentDoc = pendingSnapshot.docs[0];
    const payment = paymentDoc.data();
    const paymentId = paymentDoc.id;

    const planConfig = { day_pass: 1, monthly_vip: 30, quarterly_pro: 90 };
    const durationDays = planConfig[payment.selectedPlan] || 30;
    const expiry = new Date(Date.now() + durationDays * 24 * 60 * 60 * 1000);

    await db.collection('payments').doc(paymentId).update({
      status: 'paid',
      transactionId: transactionId,
      smsReceivedAt: new Date().toISOString(),
      verifiedAt: new Date().toISOString()
    });

    await db.collection('users').doc(payment.userId).update({
      subscriptionStatus: 'active',
      subscriptionPlan: payment.selectedPlan,
      subscriptionStart: new Date().toISOString(),
      subscriptionExpiry: expiry.toISOString()
    });

    console.log('✅ Activated for user:', payment.userId);
    res.json({ success: true, userId: payment.userId, transactionId });

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
