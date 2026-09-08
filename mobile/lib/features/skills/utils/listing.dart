/// What the author's own list says about a package they published — a 1:1
/// port of frontend-v2 `features/skills-center/lib/listing.ts`.
///
/// Two independent facts decide one chip: `publicationStatus` is the author's
/// decision (is there a release at all, or did they pull it), `listing` is the
/// operator's (is it on the shelf, queued, or refused). Rendering them as two
/// badges made a withdrawn-but-approved package read as still on sale, so they
/// collapse here into the single state the author actually cares about.
library;

import '../widgets/entry_row.dart';

/// The five states a published package can be in, from the author's side.
enum ListingChip { pending, listed, rejected, delisted, withdrawn }

/// Semantic, not decorative: `delisted` is an operator's decision the author
/// can act on, `rejected` is a refusal — the second one is the louder colour.
const Map<ListingChip, BadgeTone> listingTones = {
  ListingChip.pending: BadgeTone.warn,
  ListingChip.listed: BadgeTone.ok,
  ListingChip.rejected: BadgeTone.danger,
  ListingChip.delisted: BadgeTone.muted,
  ListingChip.withdrawn: BadgeTone.muted,
};

/// The locale key suffix, kept identical to the web's `badge.listing.*`.
extension ListingChipName on ListingChip {
  String get key => name;
}

/// The states whose reason (`listingNote`) the author must be able to read.
const Set<ListingChip> _explained = {ListingChip.rejected, ListingChip.delisted};

bool explainsItself(ListingChip chip) => _explained.contains(chip);

/// The chip for one personal package, or null when there is nothing to say.
///
/// A draft that was never submitted gets no chip: "not uploaded" is already
/// the row's other badge, and a second one repeating it in operator vocabulary
/// is noise. An absent [listing] means an older backend that had no shelf
/// states, where every published package was by definition listed.
ListingChip? listingChipFor(String? publicationStatus, String? listing) {
  if (publicationStatus == 'withdrawn') return ListingChip.withdrawn;
  if (publicationStatus != 'published') return null;
  switch (listing) {
    case 'pending':
      return ListingChip.pending;
    case 'rejected':
      return ListingChip.rejected;
    case 'delisted':
      return ListingChip.delisted;
    default:
      return ListingChip.listed;
  }
}

/// Whether publishing again is a re-submission rather than a first upload.
///
/// A refusal and a delisting both leave a release the operator has already
/// ruled on, and so does a withdrawal — the button that sends it back should
/// not claim to be uploading something new.
bool isResubmission(ListingChip? chip) =>
    chip == ListingChip.rejected ||
    chip == ListingChip.delisted ||
    chip == ListingChip.withdrawn;

/// Whether the author still has a release the store could be showing.
bool canWithdraw(ListingChip? chip) =>
    chip != null && chip != ListingChip.withdrawn;
