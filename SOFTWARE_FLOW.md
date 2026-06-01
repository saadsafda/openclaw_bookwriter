# OpenClaw Book Writer - Step by Step Software Flow

This document is a complete, step by step guide for the full software flow.
It is written for non-technical users and focuses on the web UI.

---

## 0) One-time setup (first run only)

1. Start the server:
   - Open Terminal in the project folder.
   - Run:
     - source .venv/bin/activate
     - python3 app.py
2. Open the app in your browser:
   - http://127.0.0.1:5000
3. Keep the Terminal window open while you work.

Screenshot placeholder:
- App running in Terminal
- Browser open on the home page

---

## 1) Write tab - create the book

1. Prepare your outline document (.docx).
   - Use headings like "Chapter 1" and subheadings like "- Topic".
2. On the Write tab, upload the .docx file.
3. Set your writing options (tone, word counts, images).
4. Click Generate / Write.
5. Wait until the status shows success.
6. Download the files:
   - Final file
   - Kindle file
   - Paperback file

Optional (recommended):
7. Use the Book Editor to fix any text issues.
8. Use Replace Image if you want new illustrations.
9. Generate listing (subtitle, description, categories) if needed.

Screenshot placeholder:
- Write tab before upload
![Write tab before upload](screenshots/01-write-tab.png)
- Options panel filled
- Progress while generating
- Success state with download buttons

---

## 2) Publish on KDP (manual step)

This step is always manual and happens on Amazon KDP.
OpenClaw does not publish books for you.

1. Upload the Kindle file in KDP.
2. Upload the Paperback file in KDP.
3. Submit both for review.
4. Wait until the book goes live.
5. Copy the ASIN and product URL for each marketplace (US, UK, CA, AU).

Screenshot placeholder:
- KDP upload screen
- KDP live status with ASIN visible

---

## 3) Launch tab - create a publication

A draft publication is created automatically after a successful book run.

1. Open the Launch tab.
2. Click the publication card to open details.
3. Check the title, subtitle, and description.
4. Add the ASIN and product URL for each marketplace.
5. Save changes.

Screenshot placeholder:
- Launch tab list of publications
![Launch tab list of publications](screenshots/05-launch-tab.png)
- Publication detail modal with metadata
- Marketplaces section filled

---

## 4) Amazon Ads (optional)

1. In the Launch tab header, click Accounts.
2. Confirm your Amazon Ads account is connected.
3. In the publication detail modal, open the Amazon Ads section.
4. Select campaign type, budget, and marketplaces.
5. Click Launch on selected marketplaces.

Tip: Start in PAUSED state, then enable in Amazon Ads after review.

Screenshot placeholder:
- Accounts modal with profiles
- Amazon Ads panel in publication detail

---

## 5) Review automation (post-purchase emails)

This is for follow-up review requests. Nothing is sent automatically.

1. In the publication detail modal, open Review automation.
2. Set From name and From email.
3. Edit templates if needed, then Save settings.
4. Add recipients (one by one or CSV upload).
5. Review scheduled emails in the list.

You have two ways to send:

A) MailerLite (drafts only)
- Click Push to MailerLite (drafts).
- Open MailerLite and publish the draft campaigns.
- Note: MailerLite requires the Advanced plan to submit content by API.

B) Manual send
- Open a scheduled email.
- Copy the subject/body or download the .eml file.
- Send it in your email tool.
- Click Mark as sent.

Screenshot placeholder:
- Review settings panel
- Recipients list
- Scheduled emails list
- Push to MailerLite button
- Manual send dialog

---

## 6) Common issues and fixes

- "Content submission is only available on advanced plan"
  - Your MailerLite plan does not allow API content creation. Upgrade to Advanced or use manual send.
- "Subscribers synced: 0"
  - There are no pending recipients (they may already be reviewed or unsubscribed).
- No campaigns created
  - Check that From name/email are set and verified.

Screenshot placeholder:
- Example warning popup

---

## 7) End-to-end checklist (quick view)

1. Start server and open the app.
2. Write tab: upload outline, generate book, download files.
3. KDP: upload Kindle and Paperback, wait for live status.
4. Launch tab: add ASINs and URLs, save.
5. (Optional) Amazon Ads: launch campaigns in PAUSED state.
6. Review automation: add recipients and send draft emails.

---

## 8) Where to place screenshots

Save screenshots into a folder called screenshots and use these names:
- screenshots/01-write-tab.png
- screenshots/02-options.png
- screenshots/03-generating.png
- screenshots/04-success-downloads.png
- screenshots/05-launch-tab.png
- screenshots/06-publication-details.png
- screenshots/07-marketplaces.png
- screenshots/08-accounts.png
- screenshots/09-amazon-ads.png
- screenshots/10-review-settings.png
- screenshots/11-recipients.png
- screenshots/12-scheduled.png
- screenshots/13-mailerlite-push.png
- screenshots/14-manual-send.png

Then update this document to replace each "Screenshot placeholder" with a real image link.
