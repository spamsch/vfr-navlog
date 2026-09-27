import { describe, expect, it } from "vitest";

import { destination } from "./geo";
import { analyseLeg, candidatesForLeg, gcBearing, radialAt, type Vor } from "./vor";

const vor = (lat: number, lon: number, extra: Partial<Vor> = {}): Vor => ({
  ident: "TST", name: "Test", freq: "115.00", lat, lon, var: 0, dme: true, range_nm: 80, route_dist_nm: 0, ...extra,
});

// A 20 NM leg heading due north.
const A = { lat: 51.0, lon: 7.0 };
const B = { lat: 51.0 + 20 / 60, lon: 7.0 };

describe("radialAt", () => {
  it("is the magnetic bearing from the station, using the station's variation", () => {
    const v = vor(51.0, 7.0, { var: 3 });
    expect(radialAt(v, B)).toBe(357); // true 000 − 3°E
    expect(radialAt(vor(51, 7), { lat: 51, lon: 7.5 })).toBe(90);
  });
});

describe("analyseLeg", () => {
  it("station behind the leg start: outbound on the radial, FROM", () => {
    const v = vor(A.lat - 10 / 60, 7.0, { var: 2 });
    const c = analyseLeg(v, A, B);
    expect(c.flag).toBe("FROM");
    expect(c.obs).toBe(358); // course 000 true, 2°E
    expect(c.radialLabel).toBe("outbound R358");
    expect(c.maxDev).toBeLessThan(0.5);
    expect(c.trackable).toBe(true);
  });

  it("station beyond the waypoint: inbound, TO", () => {
    const c = analyseLeg(vor(B.lat + 5 / 60, 7.0), A, B);
    expect(c.flag).toBe("TO");
    expect(c.obs).toBe(0);
    expect(c.radialLabel).toBe("inbound R180");
    expect(c.trackable).toBe(true);
  });

  it("station on the leg: TO then FROM, trackable through station passage", () => {
    const c = analyseLeg(vor(A.lat + 10 / 60, 7.0), A, B);
    expect(c.flag).toBe("TO→FROM");
    expect(c.trackable).toBe(true);
  });

  it("station well abeam: not trackable, radials still give progress", () => {
    const abeam = destination({ lat: A.lat + 10 / 60, lon: 7.0 }, 90, 10);
    const c = analyseLeg(vor(abeam.lat, abeam.lon), A, B);
    expect(c.trackable).toBe(false);
    expect(c.maxDev).toBeGreaterThan(30);
    expect(c.rStart).not.toBe(c.rEnd);
  });

  it("small offset stays within half scale; larger offset does not", () => {
    const near = destination({ lat: A.lat - 20 / 60, lon: 7.0 }, 90, 1); // 1 NM off, 20 NM behind
    expect(analyseLeg(vor(near.lat, near.lon), A, B).trackable).toBe(true);
    const far = destination({ lat: A.lat - 5 / 60, lon: 7.0 }, 90, 2); // 2 NM off, 5 NM behind
    expect(analyseLeg(vor(far.lat, far.lon), A, B).trackable).toBe(false);
  });

  it("out of range stations are not trackable", () => {
    const c = analyseLeg(vor(A.lat - 30 / 60, 7.0, { range_nm: 25 }), A, B);
    expect(c.inRange).toBe(false);
    expect(c.trackable).toBe(false);
  });
});

describe("candidatesForLeg", () => {
  it("puts trackable stations first and drops out-of-range ones", () => {
    const abeam = destination(A, 90, 8);
    const list = candidatesForLeg(
      [
        vor(abeam.lat, abeam.lon, { ident: "ABM" }),
        vor(A.lat - 10 / 60, 7.0, { ident: "BHD" }),
        vor(A.lat - 90 / 60, 7.0, { ident: "FAR", range_nm: 40 }),
      ],
      A,
      B,
    );
    expect(list.map((c) => c.vor.ident)).toEqual(["BHD", "ABM"]);
  });
});

describe("gcBearing", () => {
  it("matches cardinal directions", () => {
    expect(gcBearing(A, B)).toBeCloseTo(0, 5);
    expect(gcBearing(B, A)).toBeCloseTo(180, 5);
  });
});
