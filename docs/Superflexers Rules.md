# League Rules

# League overview

The purpose of this document is to establish a fun, fair, and viable league. If a rule is not explicitly stated, interpretations will consider these criteria and new rules will be voted on and documented here.

* 12 teams playing a 13 game regular season with 3 weeks of playoffs  
* 29 roster spots  
* 5 taxi squad spots  
* 11 starters: 1QB, 2RB, 3WR, 1TE, 2FLEX, 1SUPERFLEX, 1K  
* A 5 round supplemental draft completed before the start of the preseason  
* Supplemental picks determined by optimal lineup points  
* Continuous free agent auction budget. Waivers process at noon everyday, except Mondays of weeks that have games.  
* Payout for playoffs and total points.  
* In order to draft you must pay the $10 league dues.  
* The league will be named The Superflexers unless we vote on a new name, cause this one was just what I put into MFL as a placeholder and then never bothered to come up with a new one.  
* Don’t be a dick.  
* Recognize that this isn’t a super hardcore league but it isn’t a super casual league either.

# Starting Lineup and Rosters

Starting lineups will consist of 11 players

* 1 QB  
* 2 RB  
* 3 WR  
* 1 TE  
* 2 FLEX (RB/WR/TE)  
* 1 SUPERFLEX (QB/RB/WR/TE)  
* 1 Kicker

Each team will have 18 bench spots and 5 taxi spots.

After the rookie draft, 5 bench spots will be added. They will be removed a week before the first NFL game at which point each team must cut down their roster to only have 18 bench players.

Player positions will be determined by Sleeper. If Sleeper changes a player’s position the change shall not be overruled under any circumstances. Owners are responsible for knowing if a player’s position may change.

Players are not locked into a team’s starting lineup until the game in which the player is participating in has kicked off, at which point they cannot be removed.

Owners unable to set their team’s lineup are to inform the commissioner and designate someone to set their lineup. Should a lineup not be submitted, barring extreme circumstances, it will be left as is.

The commissioner reserves the right to submit competitive lineups, chosen based on Sleeper’s projected points, if deemed necessary to maintain league integrity (ie: a team out of the playoff hunt starting 5 injured players in week 13 against a team still in the playoff hunt).

# Scoring

Scoring settings are shown on Sleeper.

They are standard except for:

* 0.5 points per reception  
* 3 \+ 0.1\*(yards past 30\) for field goals  
* Fumbling is \-1 point if the fumble is recovered by the offense and \-2 points if the fumble is recovered by the defense

# Annual Rookie Draft

Every year will have a linear draft for rookies. 

Draft order:  
The last pick of each round goes to the owner who won the most money, the second to last pick is given to the owner who won the second most money, and so on for the owners that won money. In the case that two owners won the same amount of money, the team with the more potential points (the total number of points you would have scored if you started your highest-scoring lineup each week) will have the later draft pick. The teams that didn’t win money are sorted by potential points, i.e. the remaining team with the fewest potential points has the first pick.

The draft is to occur in June or July, all owners will be given 24 hours to consider their options before making or trading a pick. If an owner does not pick within the 24 hours the draft will continue and when the owner realizes they didn’t pick they can contact the commissioner about choosing from the remaining players.

The clock will reset to 12 hours when an owner on the clock trades out of the pick.

The commissioner will do their best to give a warning to an owner when there is less than an hour on the clock.

# Taxi Squad

Each owner will be given 5 taxi squad slots. The only players eligible for the taxi slots are rookies that you take in the rookie draft. They cannot be placed in your starting lineup unless they are activated. Once activated a player cannot be put back into your taxi squad. 

After 3 seasons they have to be activated or dropped. 

Taxi squad players are tradeable, however any player you receive as part of a trade cannot be placed on your taxi squad.

Other owners can claim a player from your taxi squad by offering a trade of the player for a draft pick from the round the player was drafted in plus a one round higher draft pick in the next draft. Should a claim be made, the owner of the claimed player will have 72 hours to either activate the claimed player or the trade will be completed manually by the commissioner. Examples:

* During the 2020 season a player is drafted in the 2020 3rd round and placed in a taxi slot. This player can be claimed for a 2021 3rd and 2021 2nd.  
* A taxi squad player drafted in the 1st round costs two 1st round picks in the next draft. 

Claiming will not be in place until the 2020 season due to us not having a rookie specific draft.  

Taxi squad decisions must be made by the end of the last game of the first week of NFL preseason games.

## Commissioner clarification

Posted in Discord, and the reading the bot implements (`lib/taxi_rules.py`):

> So it seems like sleeper doesn't have a way to actually enforce the taxi squad rules so I'll be checking manually. As a reminder, the only players you can put on your taxi during an off-season are players you took in the rookie draft **that off-season** (so no free agent pick ups). Once they're taken off they can't be put back, and once they are in **year 4** they must be taken off.

Two things this settles that the text above leaves open:

* **The addition window is one off-season wide, not three.** A player drafted in 2024 may *remain* on a slot through 2026, but if he wasn't placed there in 2024 he can never be placed there at all.
* **The draft year counts toward the 3 seasons.** "Year 4" means years 1–3 on a slot are legal, so a 2023 draftee is a violation in 2026.

# Free Agent Acquisition Budget (Kohl’s Cash)

Fake money, called Kohl’s Cash, will be allocated to every team for them to spend on free agents  on waivers during the season. Waiver claims are awarded to the highest bid. There is a tie-breaker that works like rolling waivers where every time an owner wins a bid they move to last place in the tie-breaker. Waivers are processed in order by highest bid. 

In the case of multiple ties at the same price control is given to Sleeper because I emailed them about how that works and they didn’t seem to know.

$200 is awarded to every team immediately after the draft.

Kohl’s Cash is tradeable.

They will open at 3 am a day or two after the supplemental draft and close after the league championship.

If for some unforeseen reason a player is taken outside of that time period the transaction will be undone.

# Trades

Trades are not allowed from week 12 until the league is renewed on Sleeper after the NFL Superbowl.

Owners may trade players, draft picks, and FAAB.

Owners may only trade draft picks for future seasons that they have paid the league dues for.

Trades cannot be rescinded except for owner error. Trades will only be revoked because of legitimate owner error that is reported in a timely manner. Owners may not request that a trade be voided due to buyer’s remorse, seller’s remorse, or failure to perform due diligence before proposing or accepting a trade.

Conditional trades are allowed between owners but the terms will not be enforced by the commissioner until it is voted on during the offseason. But don’t be a dick.

When trading a player with a rapidly changing situation, it is recommended to make sure that both sides of the trade are aware of the situation before it is accepted by both sides. It is not enforced, but do not leave trades open hoping that a player’s situation will change and then accept them if it happens.

# Team Ownership

The league will consist of 12 teams without any divisions

If an owner should choose not to return the commissioner will find a replacement owner.

# Schedule

Weeks 1-11:  
Every owner will play against every other owner.

Week 12:  
Owners will play against a team randomly chosen by Sleeper when the schedule is generated, although I believe it is always the team they play in week 1\.

Week 13:  
Owners will play against their rival.

Week 14:  
Owners will play against another owner in their rivalry division. Each year the division opponent changes.

# Rivalries and Divisions

Rivals will consist of owners that have a logical connection to each other and will play twice every season.

Divisions are pairs of rivalries.

Every 5 years (2025, 2030, etc) there will be an opportunity for realignment where owners are paired by frequency of postseason matchup. Owners are allowed to refuse the new pairing to remain rivals with their current rival or find a new rival, on the condition that both owners agree.

The current rivalries and divisions are:  
Loudon Division:  
Aaron \- James  
Rob 1 \- Rob 2  
Herndon Division:  
Brandon \- Jimmy  
Corey \- Fuzzy  
Cornell Division:  
Aneesh \- Kalani  
David \- Grant

# Playoffs and Playoff Champion

The playoffs will consist of 6 teams and be played on weeks 15, 16, and 17\.

The first 4 seeds will be determined by record, using points scored as a tiebreaker. The 5th and 6th seeds will be the two remaining teams with the most points scored.

The 1st and 2nd seeds will get a bye in the first round.

The team with the most points in each head-to-head matchup will win, should a tie occur then the higher seeded team will win.

# Payout Structure

Money shall be awarded to the highest scoring teams and the highest placing playoff teams with the following structure:  
$30 \- 1st in playoffs  
$30 \- 1st in points  
$20 \- 2nd in playoffs  
$20 \- 2nd in points  
$10 \- 3rd in playoffs  
$10 \- 3rd in points

# League Trophy

There will be a single trophy that looks like the Lombardi Trophy consisting of two parts, the base and the football. The base will be awarded to the League Points Champion, the owner who scores the most points, and the ball will be awarded to the League Playoff Champion.

It will be passed between owners every year.

I am going to 3d print it, but the filament hasn’t shipped for over a month cause of the whole pandemic thing. You can see the color it will be here: [https://fillamentum.com/products/pla-extrafill-wizards-voodoo](https://fillamentum.com/products/pla-extrafill-wizards-voodoo)

# Consolation bracket

There is a consolation bracket with the 6 teams that don’t make the playoffs. 

The winner of it gets the 13th pick of the 5th round in the following rookie draft, it is not allowed to be traded.

# Changing League Rules

Rules will be proposed and voted on during the offseason.

A 2/3rd majority of cast votes are required for a rule to be accepted. 

Proposals and voting will take place on google documents that will be pinned in the Sleeper chat. 

Rule proposals will be put in a google document for review and discussion to refine them. 

For a proposal to be voted on it must be transferred to the official voting form before February starts.

Voting will end when March starts.

If a rule has more than one variation that passes, then the variation with the most votes will succeed. If the variations have the same number of votes, there will be a week-long runoff vote where owners can vote on the options again. Should this once more result in a tie, then they will be put head to head and the vote with the majority will be enacted. If this once more results in a tie then the commissioner will decide on a path forward based on the specifics of the rule.

# League Dues

League dues are $10 per season per team and are non-refundable, except in the case that no fantasy season occurs.

For an owner to draft they must have paid the league dues. *Exception for the 2020 season below due to how late I wrote this.*

For the 2020 season dues must be paid by the end of the NFL preseason.

If an owner leaves the league for any reason, their dues will be forfeit to the prize pool. Replacement owners may reimburse the owner that left but they are under no obligation to.

If an owner cannot reasonably meet the dues deadline they must notify the commissioner and make alternate arrangements before the deadline. 

League finances will be tracked on this spreadsheet: [https://docs.google.com/spreadsheets/d/1SBbMfdaxLU5F3vHf3cC0\_icmXF38I2R12c8-nA2reLc/edit?usp=sharing](https://docs.google.com/spreadsheets/d/1SBbMfdaxLU5F3vHf3cC0_icmXF38I2R12c8-nA2reLc/edit?usp=sharing&authuser=0)

# Ice punishments

* Tanking or negligence \- Failure to start a full lineup of active players, excluding game day decisions will result in an Icing  
* Commissioner neglect \- Corey gets iced if he fails to post power rankings before Thursday night during the regular season.

# Owner Responsibilities

* Owners are responsible for ensuring their team has current lineups.  
* Owners are responsible for knowing and following the rules and schedule listed in these bylaws as well as on the League Site.  
* Owners are responsible for responding to emails or private messages from the commissioner and other owners in a timely manner.  
* Owners are responsible for taking part in league votes and debates.  
* Owners are responsible for regularly accessing Sleeper.  
* Owners are responsible for responding to trade offers in a timely manner.  
* Owners are responsible for participating in all required league functions and scheduled events, whether they occur in the regular season or offseason.  
* Owners are responsible for submitting weekly lineups.  
* Owners are responsible for notifying the commissioner if they will be away for an excessive period of time so arrangements can be made to set lineups and/or manage the team during that owner’s absence.

# Commissioner Responsibilities

* The commissioner will act in good faith at all times to maintain a fun, fair, and competitive environment.  
* The commissioner will have final authority over all changes necessary to maintain the integrity of the league while respecting the owners and the league rules.  
* The commissioner will maintain league logistics  
* Set lineups when requested  
* Correct clear and obvious owner mistakes  
* Facilitate the drafts  
* Collect league dues and give payouts  
* Void trades if rules are clearly violated  
* Put sanctions and owner removals up for vote in the case of anti-competitive conduct  
* Control abandoned or otherwise orphaned franchises while seeking new ownership  
* Maintain the league on Sleeper

# Anti-Competitive Conduct

Anti-competitive conduct is defined as owners or teams engaging in conduct that prevents, reduces or otherwise negatively affects the natural competition and well-being of the league. The following anticompetitive actions are strictly prohibited. Violations of these rules shall be subject to sanctions that require nine votes to be enforced.

**Tanking**: Owners are expected to use their best efforts to set their best available lineup every week of competition, even if they are well out of playoff contention. Tanking is defined as failing to submit their best available starting lineup either intentionally or through indifference. It is understood that owners may play hunches on who to start and won’t always start the player who scores the most points.

However, an owner who knowingly benches star players or obvious starting players in favor of players who are marginal, clearly injured, benched, suspended or on their bye weeks shall be subject to commissioner's sanctions for a first offense. A second offense shall result in that owner’s immediate removal from the league.

**Anti-competitive Trades**: Owners may not make trades that result in worsening their own team in order to stock another team playing a third team they want or need to lose. Owners may not make trades if they do not intend to return to the league for the next season. If an owner does not intend to return to the league, they should announce their retirement publicly and play out the season using their best efforts; the incoming replacement owner should be permitted to inherit an intact team and make their own trades and roster decisions. The commissioner may retroactively void a trade if there is clear and convincing evidence that the trade was prohibited on anti-competitive grounds that were not apparent at the time of the trade.

**Collusion**: Collusion is defined as two or more owners making arrangements and/or acting in concert to influence the results of league activities such as game outcomes, draft standing or player availability. Any unsportsmanlike conduct coordinated between two or more owners is considered to be collusion, as is teams trading to consolidate better players on one team and/or agreeing to share payouts by acting in concert. Trading a player with the condition that he be traded back would be considered collusion, and against the rules. Owners engaging in collusion are subject to penalties and/or immediate removal from the league.

**Dumping**: An owner who, without good reason, cuts players from their team who are obviously valuable is subject to sanctions. If an owner continues to dump players after sanctions are imposed, the owner shall be removed from the league. It is understood that teams can and will drop “borderline” players from their rosters, but dropping obvious fantasy starters or large amounts of average players in an attempt to increase draft position, make players available to others by way of collusion or sabotage the integrity of the league shall not be tolerated.

**Indifference**: An owner who fails to submit a starting lineup or fails to replace inactive players who are injured, benched, suspended or on their bye week is subject to sanctions for a first offense. An owner who fails to set a starting lineup due to indifference twice in the same season is subject to removal from the league.