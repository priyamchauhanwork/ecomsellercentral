# Known limitations (Phase 0)

Share this list with the pilot seller before onboarding (#37, #39).

## Orders

- **"Mark shipped" and "Mark delivered" don't update Shopify.** They change the status in SellerCentral only (with a timeline entry); no fulfillment is created on Shopify, the customer gets no shipping email from Shopify, and Shopify still shows the order as unfulfilled. Fulfil the order in Shopify as well. (#18)
- **A Shopify update never undoes a manual status.** Shopify can only move an order forward (pending → shipped → delivered, or cancelled), so an order you marked shipped stays shipped even if Shopify still says unfulfilled. (#9)
- **Manual status changes only go forward:** pending → shipped / delivered / cancelled, shipped → delivered. A mistaken "shipped" can't be undone from the app. (#10)
