import Nav from "@/components/Nav";
import Hero from "@/components/Hero";
import Route from "@/components/Route";
import Difference from "@/components/Difference";
import WorldScope from "@/components/WorldScope";
import People from "@/components/People";
import Proof from "@/components/Proof";
import Journal from "@/components/Journal";
import Consultation from "@/components/Consultation";
import Footer from "@/components/Footer";

export default function Home() {
  return (
    <div id="top">
      <Nav />
      <main>
        <Hero />
        <Route />
        <Difference />
        <WorldScope />
        <People />
        <Proof />
        <Journal />
        <Consultation />
      </main>
      <Footer />
    </div>
  );
}
