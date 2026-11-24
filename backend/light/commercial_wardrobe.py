"""Use complementary actual costume photographs for a gesture and its lower-body wardrobe."""

from pydantic import BaseModel, Field, model_validator

from light import reference_quality


class Coverage(BaseModel):
    same_person_and_running_outfit: bool
    true_product_support_and_position: bool
    neutral_hand_ready_to_move: bool
    contact_pose_matches: bool
    legs_and_shoes_match_and_are_visible: bool
    lower_crop_is_independent_of_old_hand: bool
    lower_box: list[int] | None = Field(min_length=4, max_length=4, description=(
        "Image 2 only: left,top,right,bottom normalized to 0..1000. Include identifiable lower clothing, "
        "both legs and shoes, excluding the upper-body pose."))
    shoes_box: list[int] | None = Field(min_length=4, max_length=4, description=(
        "Image 2 only: tight bounds enclosing both actual shoes in the lower_box, normalized to 0..1000."))
    observation: str = Field(min_length=1, max_length=600)

    @property
    def accepted(self):
        return all((self.same_person_and_running_outfit, self.true_product_support_and_position,
                    self.neutral_hand_ready_to_move, self.contact_pose_matches,
                    self.legs_and_shoes_match_and_are_visible, self.lower_crop_is_independent_of_old_hand))

    @model_validator(mode="after")
    def check_visible_coverage(self):
        if self.accepted and (self.lower_box is None or self.shoes_box is None):
            raise ValueError("Supply the photographed lower-body and footwear bounds for an approved wardrobe.")
        for box in (self.lower_box, self.shoes_box):
            if box and not (0 <= box[0] < box[2] <= 1000 and 0 <= box[1] < box[3] <= 1000):
                raise ValueError("Use ordered bounds within the full-body photograph.")
        if self.accepted:
            left, top, right, bottom = self.shoes_box
            if not (self.lower_box[0] <= left and self.lower_box[1] < top and
                    self.lower_box[2] >= right and self.lower_box[3] >= bottom):
                raise ValueError("Both shoes and the visible lower garment must fit inside the lower-body crop.")
        return self


async def check_views(ctx, plan, pictures):
    return await reference_quality.ask(
        ctx, "commercial_running_wardrobe_views", Coverage,
        "Inspect the actual photographs. Image 1 supplies the running actor's face, torso, neutral gesture hand, "
        "and the supported product. Image 2 supplies the same running outfit below the hips and its footwear. "
        "Image 3 is the established running contact pose. Images 4 and 5 are the corresponding commuting references. "
        "The remaining views are the merchant's material. Compare identities, colours, garment details, actual product, "
        "supporting hand or carrier, side and reach. In image 1 the gesturing hand must genuinely be at rest, visibly "
        "clear of the contact surface or with its active finger naturally retracted if that hand must also bear weight. "
        "In image 3 the hand actually reaches the intended feature as in image 5. In image 2 check both real shoes, legs "
        "and lower clothing. Identify a crop showing these, overlapping at least one garment visible in image 1, "
        "without the old upper-body gesture. Reject incompatible actors, clothing, product, pose or hidden footwear, "
        "or a hand or product that would be contradicted by using the two references together. Cropping a view is "
        "allowed when another actual view covers the required details. Describe what is visible in the production language.",
        {"language": plan.language, "product": plan.product, "appearance": plan.appearance,
         "wardrobe": plan.track_wardrobe, "carry": plan.carry, "contact": plan.contact}, pictures,
        max_tokens=1400, timeout=70)


async def resolve(ctx, prefix, run, plan, library, material):
    from light import commercial_film as film

    if getattr(plan, "score_revision", 1) != 2 or not library.get("contact_actor"):
        return False
    poses = [library["contact_actor"], *material]
    bases = {library.get("resting_carried_actor"), library.get("carried_actor")}
    full, cropped = [], []
    decisions = run.get("director_decisions") or {}
    for key, value in decisions.items():
        if not key.startswith(f"{film.recipe.REVIEW}:track-wardrobe:"):
            continue
        pictures, report = value.get("evidence") or [], value.get("report") or {}
        if (len(pictures) < 4 or pictures[2] not in bases or pictures[3:] != poses or report.get("ok")
                or report.get("pose_correction") or key != f"{film.recipe.REVIEW}:track-wardrobe:{film._fingerprint(pictures, plan.track_wardrobe)}"):
            continue
        (full if report.get("shoes_box") else cropped).append(pictures)
    for upper in reversed(cropped):
        for donor in reversed(full):
            if upper[0] == donor[0] or upper[1:] != donor[1:]:
                continue
            pictures = [upper[0], donor[0], *upper[1:]]
            key = f"{film.recipe.REVIEW}:track-coverage:{film._fingerprint(pictures, plan.track_wardrobe, plan.carry, plan.contact)}"
            answer = decisions.get(key)
            if not answer:
                answer = await check_views(ctx, plan, pictures)
                answer = await film._record(ctx, prefix, run, 12, key, answer)
            check = Coverage.model_validate(answer["report"])
            if check.accepted:
                library["track_actor"] = upper[0]
                library["track_contact"] = upper[1]
                library["track_lower_body"] = await film._shoe_detail(ctx, donor[0], check.lower_box, lower_body=True)
                library["track_feet"] = await film._shoe_detail(ctx, donor[0], check.shoes_box)
                return True
    return False
