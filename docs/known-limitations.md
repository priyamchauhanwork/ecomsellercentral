# Known limitations (Phase 0)

Share this list with the pilot seller before onboarding (#37, #39).

## Orders

- **"Mark shipped" and "Mark delivered" don't update Shopify.** They change the status in SellerCentral only (with a timeline entry); no fulfillment is created on Shopify, the customer gets no shipping email from Shopify, and Shopify still shows the order as unfulfilled. Fulfil the order in Shopify as well. (#18)
- **A Shopify update never undoes a manual status.** Shopify can only move an order forward (pending → shipped → delivered, or cancelled), so an order you marked shipped stays shipped even if Shopify still says unfulfilled. (#9)
- **Manual status changes only go forward:** pending → shipped / delivered / cancelled, shipped → delivered. A mistaken "shipped" can't be undone from the app. (#10)

## Products

- **A single SKU can't be hidden on Shopify.** Taking one variant off sale from the app shows it as *sold out* on the storefront (quantity 0, overselling off, stock tracking turned on) rather than removing it; putting it back on sale restores the app's stock figure. (#47)
- **Taking a product off sale makes it a draft on Shopify,** which hides it from every sales channel, not only the Online Store. (#47)
- **Product images are links, not uploads.** (#45)
- **Products can't be edited in the app yet;** edit them in Shopify and re-import, or create a new one. (#45)

## Inventory

- **One stock location** (Shopify's primary location); purchase orders and several warehouses come later. (#49)
- **"Delivered" comes from the courier's status in Shopify, or from Mark delivered.** Shopify only knows a parcel was delivered when the shipment has tracking; until then the units stay *shipped* (still on hand, not sellable). Mark delivered in the app when needed. A customer return also counts the units as delivered. (#52)
- **Acknowledging is app-only.** Shopify has no "processing" state; if Shopify cancels or cuts an acknowledged order, it is applied and the order is flagged *Needs attention*. (#52)
- **RTOs are entered by hand** on the order page; Shopify has no RTO concept. (#55)
- **QC results are final.** For a Shopify return they are reported to Shopify, which doesn't allow changing a disposition; this needs the `write_returns` scope on the Shopify app. A return must be approved in Shopify before its QC can be reported. (#54)
- **Refunds don't move stock any more;** only returns (approved, received, QC'd) do. (#49)
- **Cancelling in Shopify without restocking still frees the units here** (they were never shipped, so they are still on the shelf). (#49)
- **Orders placed before your products were imported don't hold stock,** because the imported figure already reflected them. Their lines say so on the order page. (#21)
- **The app's figure wins on Shopify.** Adjusting stock in the app sets Shopify's available quantity to the app's figure. Edit stock in the app, not in the Shopify admin, or push the app's figure from the inventory page. (#20)
- **A sale on Shopify in the seconds between its order and the app receiving it can be overwritten** by a manual adjustment made in exactly that window; the inventory page then shows the difference. (#20)
